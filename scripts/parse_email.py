"""
scripts/parse_email.py
======================
Phase 1 — Email intake and lead creation.

Accepts either:
  - A raw .eml file on disk (for testing / batch mode)
  - IMAP polling for new emails in the designated inbox

For each qualifying email:
  1. Assigns a lead number (P[YY][MM][NNNNN])
  2. Extracts lead metadata via Gemini
  3. Saves PDF attachments to inputs/pdfs/{lead_no}/
  4. Classifies each PDF and records it in lead_pdfs table
  5. Creates a row in the leads table

Usage:
    python scripts/parse_email.py --eml path/to/email.pdf
    python scripts/parse_email.py --poll        (IMAP polling loop)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.db import get_conn

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Settings loader
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "settings.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Lead number assignment (Phase 1.2)
# ---------------------------------------------------------------------------

def next_lead_no() -> str:
    """
    Query leads table for the latest lead_no and auto-increment.
    Format: P[YY][MM][NNNNN]
    """
    today = date.today()
    prefix = today.strftime("%y%m")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lead_no FROM leads WHERE lead_no LIKE %s ORDER BY lead_no DESC LIMIT 1",
                (f"P{prefix}%",),
            )
            row = cur.fetchone()

    if row:
        last_seq = int(row[0][5:])  # digits after P[YY][MM]
        seq = last_seq + 1
    else:
        seq = 1

    return f"P{prefix}{seq:05d}"


# ---------------------------------------------------------------------------
# LLM email parsing (Phase 1.3)
# ---------------------------------------------------------------------------

def parse_email_with_llm(email_text: str, api_key: str) -> dict:
    """
    Extract lead metadata from an email via Gemini.
    Returns a dict with all EmailMetadata fields.
    Falls back to regex extraction if Gemini is unavailable.
    """
    if not api_key:
        log.warning("GEMINI_API_KEY not set — using regex fallback for email parsing.")
        return _regex_email_parse(email_text)

    prompt = f"""You are an industrial RFQ intake agent.
Extract the following fields from the email text below and return a JSON object.
Use null for any field not found.

Fields to extract:
- customer_name: the company sending the RFQ
- contact_person: the sender's full name
- contact_email: the sender's email address
- customer_rfq_ref: the customer's own reference number (e.g. C-4758, RFQ-2024-001)
- project_name: the project name or tag (e.g. FLOWMORE, TATA POWER, BHEL)
- action_requested: what the email is asking Cenlub to do (1 sentence)
- attachments_mentioned: list of filenames mentioned in the email body
- items_requested: list of any specific equipment items explicitly named
- urgency: any deadline or urgency mentioned
- additional_context: any other relevant context (1-2 sentences max)

Email text:
---
{email_text[:8000]}
---

Return ONLY the JSON object, no explanation.
"""

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        if resp.text:
            return json.loads(resp.text)
    except Exception as exc:
        log.warning("Gemini email parsing failed: %s", exc)

    return _regex_email_parse(email_text)


def _regex_email_parse(text: str) -> dict:
    """Minimal regex fallback when Gemini is unavailable."""
    def _first(patterns: list[str]) -> str | None:
        for pat in patterns:
            m = re.search(pat, text, re.I)
            if m:
                return m.group(1).strip()
        return None

    return {
        "customer_name": _first([r"customer\s*[:\-]\s*([^\n]+)", r"purchasing\s+team\s*[-–]\s*([^\n]+)"]),
        "contact_person": _first([r"from\s*:\s*([A-Z][a-z]+\s+[A-Z][a-z]+)\s*[<\n]", r"regards[,\n\s]+([A-Z][a-z]+\s+[A-Z][a-z]+)"]),
        "contact_email": _first([r"([\w.\-+]+@[\w.\-]+\.[a-zA-Z]{2,})"]),
        "customer_rfq_ref": _first([r"\bC-(\d{4,6})\b", r"rfq\s*(?:ref|no)\.?\s*[:\-]\s*([\w\-]+)"]),
        "project_name": _first([r"project\s*(?:name)?\s*[:\-]\s*([^\n/]+)"]),
        "action_requested": None,
        "attachments_mentioned": re.findall(r"[\w\s\-]+\.pdf", text, re.I)[:10],
        "items_requested": [],
        "urgency": None,
        "additional_context": None,
    }


# ---------------------------------------------------------------------------
# PDF classification (Phase 1.4)
# ---------------------------------------------------------------------------

def classify_pdf_with_llm(pdf_text_first2pages: str, api_key: str) -> str:
    """
    Ask Gemini to classify a PDF type based on its first 2 pages.
    Returns one of: LARGE_SPEC / SPARES_LIST / MANDATORY_SPARES / BOM / OTHER
    """
    if not api_key:
        return _classify_pdf_regex(pdf_text_first2pages)

    prompt = (
        "What type of industrial document is this? "
        "Choose exactly one:\n"
        "- LARGE_SPEC (technical specification, material requisition, LOS, datasheet)\n"
        "- SPARES_LIST (O&M spare parts list, recommended spares)\n"
        "- MANDATORY_SPARES (mandatory spare parts list)\n"
        "- BOM (bill of materials with prices)\n"
        "- EMAIL (RFQ email or inquiry letter)\n"
        "- OTHER\n\n"
        f"Document text (first 2 pages):\n---\n{pdf_text_first2pages[:3000]}\n---\n\n"
        "Return ONLY the single word classification."
    )

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        if resp.text:
            result = resp.text.strip().upper().split()[0]
            valid = {"LARGE_SPEC", "SPARES_LIST", "MANDATORY_SPARES", "BOM", "EMAIL", "OTHER"}
            return result if result in valid else "OTHER"
    except Exception as exc:
        log.warning("PDF classification via Gemini failed: %s", exc)

    return _classify_pdf_regex(pdf_text_first2pages)


def _classify_pdf_regex(text: str) -> str:
    low = text.lower()
    if any(k in low for k in ["mandatory spare", "mandatory parts"]):
        return "MANDATORY_SPARES"
    if any(k in low for k in ["o&m spare", "operation and maintenance", "recommended spare"]):
        return "SPARES_LIST"
    if any(k in low for k in ["bill of material", "unit price", "item no"]):
        return "BOM"
    if any(k in low for k in ["specification", "lube oil system", "material requisition"]):
        return "LARGE_SPEC"
    if any(k in low for k in ["dear", "rfq", "inquiry", "subject:"]):
        return "EMAIL"
    return "OTHER"


# ---------------------------------------------------------------------------
# Lead creation in DB (Phase 1.2 + 1.3)
# ---------------------------------------------------------------------------

def create_lead(meta: dict, lead_no: str) -> int:
    """
    Insert a new lead row into the leads table.
    Returns the auto-generated lead ID.
    """
    date_received = None
    raw_date = meta.get("date_received") or meta.get("urgency") or ""
    if raw_date:
        for fmt in ("%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                date_received = datetime.strptime(raw_date[:11], fmt).date()
                break
            except ValueError:
                pass
    if not date_received:
        date_received = date.today()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO leads
                    (lead_no, customer_rfq_ref, project_name, customer_name,
                     contact_person, contact_email, date_received, status,
                     email_subject, email_body)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'received', %s, %s)
                ON CONFLICT (lead_no) DO NOTHING
                RETURNING id
                """,
                (
                    lead_no,
                    meta.get("customer_rfq_ref"),
                    meta.get("project_name"),
                    meta.get("customer_name"),
                    meta.get("contact_person"),
                    meta.get("contact_email"),
                    date_received,
                    meta.get("email_subject", ""),
                    meta.get("email_body", ""),
                ),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            # Lead already existed — fetch its ID
            cur.execute("SELECT id FROM leads WHERE lead_no = %s", (lead_no,))
            return cur.fetchone()[0]


def register_pdf(lead_id: int, filename: str, pdf_type: str,
                 page_count: int, file_path: str) -> int:
    """Insert a lead_pdfs row and return its ID."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO lead_pdfs
                    (lead_id, filename, pdf_type, page_count, file_path, parse_status)
                VALUES (%s, %s, %s, %s, %s, 'pending')
                RETURNING id
                """,
                (lead_id, filename, pdf_type, page_count, file_path),
            )
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Process a single email (from file or text)
# ---------------------------------------------------------------------------

def process_email(
    email_text: str,
    pdf_paths: list[str],
    gemini_key: str,
    settings: dict,
) -> dict:
    """
    Full Phase 1 pipeline for one email.
    Returns a summary dict.
    """
    # 1.2 Assign lead number
    lead_no = next_lead_no()
    log.info("Phase 1.2: Assigned Lead No. %s", lead_no)

    # 1.3 LLM email parsing
    meta = parse_email_with_llm(email_text, gemini_key)
    meta["email_body"] = email_text[:5000]
    log.info(
        "Phase 1.3: Customer=%s | Project=%s | RFQ Ref=%s",
        meta.get("customer_name"), meta.get("project_name"), meta.get("customer_rfq_ref"),
    )

    # Create lead in DB
    lead_id = create_lead(meta, lead_no)
    log.info("Phase 1.2: Lead ID=%d created in DB.", lead_id)

    # 1.4 Save and classify attachments
    inputs_base = settings.get("output", {}).get("inputs_path", "inputs/pdfs/")
    lead_pdf_dir = os.path.join(inputs_base, lead_no)
    os.makedirs(lead_pdf_dir, exist_ok=True)

    registered_pdfs = []
    for src_path in pdf_paths:
        if not os.path.exists(src_path):
            log.warning("PDF not found, skipping: %s", src_path)
            continue

        filename = os.path.basename(src_path)
        dest_path = os.path.join(lead_pdf_dir, filename)

        import shutil
        shutil.copy2(src_path, dest_path)

        # Classify via LLM (first 2 pages)
        from src.ingestion.loader import load_pdf
        doc = load_pdf(dest_path)
        page_count = len(doc["pages"])
        first2_text = "\n".join(p["text"] for p in doc["pages"][:2])
        pdf_type = classify_pdf_with_llm(first2_text, gemini_key)

        pdf_id = register_pdf(lead_id, filename, pdf_type, page_count, dest_path)
        registered_pdfs.append({
            "pdf_id": pdf_id,
            "filename": filename,
            "pdf_type": pdf_type,
            "page_count": page_count,
            "file_path": dest_path,
        })
        log.info("  Registered PDF: %s → %s (%d pages)", filename, pdf_type, page_count)

    return {
        "lead_no": lead_no,
        "lead_id": lead_id,
        "meta": meta,
        "pdfs": registered_pdfs,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="Phase 1: Email intake & lead creation")
    parser.add_argument("--eml", help="Path to .eml or PDF email file")
    parser.add_argument("--pdfs", nargs="*", default=[], help="PDF attachment paths")
    parser.add_argument("--email-text", help="Raw email text (for testing)")
    args = parser.parse_args()

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    settings = _load_settings()

    email_text = ""
    pdf_paths = list(args.pdfs or [])

    if args.eml:
        # Support reading email from a PDF (as in the raw rfq_email.pdf)
        if args.eml.lower().endswith(".pdf"):
            from src.ingestion.loader import load_pdf
            doc = load_pdf(args.eml)
            email_text = doc["full_text"]
        else:
            with open(args.eml, encoding="utf-8", errors="replace") as fh:
                email_text = fh.read()
    elif args.email_text:
        email_text = args.email_text
    else:
        # Default: use rfq_email.pdf from data/raw as a demo
        default_email = os.path.join("data", "raw", "rfq_email.pdf")
        if os.path.exists(default_email):
            from src.ingestion.loader import load_pdf
            doc = load_pdf(default_email)
            email_text = doc["full_text"]
            # Add other raw PDFs as attachments
            raw_dir = os.path.join("data", "raw")
            pdf_paths = [
                os.path.join(raw_dir, f)
                for f in sorted(os.listdir(raw_dir))
                if f.lower().endswith(".pdf") and f != "rfq_email.pdf"
            ]
        else:
            parser.print_help()
            sys.exit(1)

    result = process_email(email_text, pdf_paths, gemini_key, settings)

    print("\n── Phase 1 Complete ──────────────────────────────────────")
    print(f"  Lead No.  : {result['lead_no']}")
    print(f"  Lead ID   : {result['lead_id']}")
    print(f"  Customer  : {result['meta'].get('customer_name')}")
    print(f"  Project   : {result['meta'].get('project_name')}")
    print(f"  PDFs      : {len(result['pdfs'])}")
    for p in result["pdfs"]:
        print(f"    {p['filename']:40s} → {p['pdf_type']}")
    print("──────────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
