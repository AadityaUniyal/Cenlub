"""
scripts/parse_pdf.py
====================
Phase 2 — Large PDF parsing engine.

Handles any PDF: any size, any format, structured or scanned.
For each PDF (identified by its lead_pdfs.id), this module:

  2.1  Extracts text page-by-page (pdfplumber + PyMuPDF OCR fallback)
  2.2  Classifies each page into a zone (LINE_ITEM_LIST / DATASHEET /
       MANDATORY_SPARES / OM_SPARES / GENERAL_TEXT)
  2.3  Extracts structured table rows from table zones
  2.4  Detects equipment tag numbers (LLM-assisted regex, then fallback)
  2.5  Runs LLM chunk processing on GENERAL_TEXT zones
  2.6  Extracts technical spec key-values from DATASHEET zones
  2.7  Persists extracted_items rows to Neon DB

Usage:
    python scripts/parse_pdf.py --pdf-id 1
    python scripts/parse_pdf.py --lead-id 5   (processes all PDFs for a lead)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.db import get_conn

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "settings.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 2.2  Zone classification
# ---------------------------------------------------------------------------

def classify_page_zone(page_text: str, tables: list, settings: dict) -> str:
    """
    Classify a page into one of five zones using configurable signal patterns.
    Returns: LINE_ITEM_LIST / DATASHEET / MANDATORY_SPARES / OM_SPARES / GENERAL_TEXT
    """
    signals: dict = settings.get("pdf_parsing", {}).get("zone_signals", {})
    low = page_text.lower()

    def _has_signal(zone_key: str) -> bool:
        return any(s.lower() in low for s in signals.get(zone_key, []))

    # Check table headers too
    table_headers = ""
    for tbl in tables:
        if tbl.get("data"):
            table_headers += " ".join(str(c) for c in tbl["data"][0] if c).lower() + " "

    combined = low + " " + table_headers

    if _has_signal("MANDATORY_SPARES"):
        return "MANDATORY_SPARES"
    if _has_signal("OM_SPARES"):
        return "OM_SPARES"
    if _has_signal("DATASHEET"):
        return "DATASHEET"
    if _has_signal("LINE_ITEM_LIST") or any(
        k in combined for k in ["tag no", "item no", "sr. no", "sl. no", "sl no"]
    ):
        return "LINE_ITEM_LIST"
    return "GENERAL_TEXT"


# ---------------------------------------------------------------------------
# 2.3  Structured table extraction
# ---------------------------------------------------------------------------

def extract_table_rows(tables: list, zone: str, page_no: int) -> list[dict]:
    """
    Extract item rows from pdfplumber tables.
    Dynamically detects column roles from headers.
    Returns list of {part_description, quantity_raw, unit, remarks, source_page, source_zone}
    """
    results = []

    # Column role keywords (configurable via settings in a future iteration)
    ROLE_MAP = {
        "description": ["description", "part", "item", "component", "material"],
        "quantity":    ["qty", "quantity", "no.", "nos", "required", "nos."],
        "remarks":     ["remark", "note", "comment"],
        "id":          ["tag", "item no", "sr no", "sl", "s.no", "serial"],
        "unit":        ["unit", "uom"],
    }

    for tbl in tables:
        data = tbl.get("data", [])
        if len(data) < 2:
            continue

        headers = [str(c).lower().strip() if c else "" for c in data[0]]

        def _find_col(*keywords: str) -> int:
            for i, h in enumerate(headers):
                if any(k in h for k in keywords):
                    return i
            return -1

        desc_i = _find_col(*ROLE_MAP["description"])
        qty_i  = _find_col(*ROLE_MAP["quantity"])
        rem_i  = _find_col(*ROLE_MAP["remarks"])
        id_i   = _find_col(*ROLE_MAP["id"])
        unit_i = _find_col(*ROLE_MAP["unit"])

        if desc_i == -1:
            desc_i = 1  # fallback: second column

        for row in data[1:]:
            if not any(c for c in row if c):
                continue
            desc = str(row[desc_i]).strip() if desc_i < len(row) else ""
            if not desc or desc.lower() in ("description", "part description", "item"):
                continue

            qty  = str(row[qty_i]).strip()  if qty_i  >= 0 and qty_i  < len(row) else ""
            rem  = str(row[rem_i]).strip()  if rem_i  >= 0 and rem_i  < len(row) else ""
            tag  = str(row[id_i]).strip()   if id_i   >= 0 and id_i   < len(row) else ""
            unit = str(row[unit_i]).strip() if unit_i >= 0 and unit_i < len(row) else ""

            results.append({
                "equipment_tag":    tag,
                "part_description": desc,
                "quantity_raw":     qty,
                "unit":             unit,
                "remarks":          rem,
                "source_page":      page_no,
                "source_zone":      zone,
                "technical_spec":   None,
            })

    return results


# ---------------------------------------------------------------------------
# 2.4  Tag number extraction
# ---------------------------------------------------------------------------

_FALLBACK_TAG_PATTERN = re.compile(
    r"\b[A-Z0-9]{2,6}-[A-Z]{2}-[A-Z]{2}-[0-9]{3}[- ]?[A-Z]?\b"
)


def detect_tag_pattern(first_pages_text: str, api_key: str) -> re.Pattern:
    """
    Ask Gemini to determine the tag number regex for this specific PDF.
    Falls back to the generic pattern if Gemini is unavailable.
    """
    if not api_key:
        return _FALLBACK_TAG_PATTERN

    prompt = (
        "What format are the equipment tag numbers in this industrial document? "
        "Give me a Python regex pattern (raw string) that would match all of them. "
        "Common formats: P-101A, 411-PT-00-109, FP-001, etc. "
        "Return ONLY the regex string, nothing else.\n\n"
        f"Document text (first 10 pages):\n---\n{first_pages_text[:5000]}\n---"
    )
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        if resp.text:
            pattern_str = resp.text.strip().strip("`").strip()
            compiled = re.compile(pattern_str)
            log.info("LLM tag pattern: %s", pattern_str)
            return compiled
    except Exception as exc:
        log.warning("Tag pattern detection failed: %s", exc)

    return _FALLBACK_TAG_PATTERN


def extract_tags_from_text(text: str, pattern: re.Pattern) -> list[str]:
    """Return all unique tag numbers found in text using the given pattern."""
    return list(dict.fromkeys(m.group(0) for m in pattern.finditer(text)))


# ---------------------------------------------------------------------------
# 2.5  LLM chunk processing (GENERAL_TEXT + empty-table pages)
# ---------------------------------------------------------------------------

def llm_extract_items(page_text: str, page_no: int, api_key: str) -> list[dict]:
    """
    For unstructured pages, use Gemini to extract equipment items.
    Returns a list of item dicts.
    """
    if not api_key or not page_text.strip():
        return []

    prompt = (
        "Extract all equipment items, spare parts, or components mentioned "
        "in this industrial document page. For each, return a JSON object with:\n"
        '  {"tag_no": ..., "description": ..., "quantity": ..., "unit": ..., '
        '"specs": ..., "remarks": ...}\n'
        "Use null for missing fields. Return a JSON array. "
        "If nothing relevant is present, return []\n\n"
        f"Page text:\n---\n{page_text[:3000]}\n---"
    )
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        if resp.text:
            items = json.loads(resp.text)
            if not isinstance(items, list):
                return []
            result = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                desc = (item.get("description") or "").strip()
                if not desc:
                    continue
                result.append({
                    "equipment_tag":    item.get("tag_no") or "",
                    "part_description": desc,
                    "quantity_raw":     str(item.get("quantity") or ""),
                    "unit":             str(item.get("unit") or ""),
                    "remarks":          str(item.get("remarks") or ""),
                    "source_page":      page_no,
                    "source_zone":      "GENERAL_TEXT",
                    "technical_spec":   item.get("specs"),
                })
            return result
    except Exception as exc:
        log.warning("LLM item extraction failed (page %d): %s", page_no, exc)
    return []


# ---------------------------------------------------------------------------
# 2.6  Technical spec extraction (DATASHEET zone)
# ---------------------------------------------------------------------------

def extract_technical_specs(page_text: str, api_key: str) -> dict | None:
    """
    Extract all technical parameter-value pairs from a datasheet page.
    Returns a dict or None.
    """
    if not api_key or not page_text.strip():
        return None

    prompt = (
        "Extract all technical parameters and their values from this "
        "industrial datasheet page. Return as a flat JSON object: "
        "{\"param_name\": \"value\", ...}. "
        "If nothing relevant, return {}\n\n"
        f"Page text:\n---\n{page_text[:3000]}\n---"
    )
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
            result = json.loads(resp.text)
            if isinstance(result, dict) and result:
                return result
    except Exception as exc:
        log.warning("Technical spec extraction failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# 2.7  Persist to DB
# ---------------------------------------------------------------------------

def persist_items(lead_id: int, pdf_id: int, items: list[dict]) -> int:
    """
    Bulk-insert extracted_items rows into Neon DB.
    Returns count of rows inserted.
    """
    if not items:
        return 0

    inserted = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for item in items:
                if not item.get("part_description"):
                    continue
                # Normalize quantity to integer if possible
                qty_raw = item.get("quantity_raw", "")
                qty_numeric = _normalize_qty(qty_raw)

                tech_spec = item.get("technical_spec")
                tech_spec_json = json.dumps(tech_spec) if tech_spec else None

                cur.execute(
                    """
                    INSERT INTO extracted_items
                        (lead_id, pdf_id, equipment_tag, part_description,
                         quantity_raw, quantity_numeric, unit, remarks,
                         technical_spec, source_page, source_zone)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        lead_id, pdf_id,
                        item.get("equipment_tag") or None,
                        item["part_description"],
                        qty_raw or None,
                        qty_numeric,
                        item.get("unit") or None,
                        item.get("remarks") or None,
                        tech_spec_json,
                        item.get("source_page"),
                        item.get("source_zone"),
                    ),
                )
                inserted += 1

        # Mark PDF as complete
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE lead_pdfs SET parse_status = 'complete' WHERE id = %s",
                (pdf_id,),
            )

    return inserted


def _normalize_qty(raw: str) -> Optional[int]:
    """Convert quantity text to integer. Returns None if not parseable."""
    if not raw:
        return None
    m = re.search(r"\b(\d+)\b", str(raw))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_pdf(lead_id: int, pdf_id: int, file_path: str, settings: dict) -> int:
    """
    Full Phase 2 pipeline for one PDF file.
    Returns the number of items extracted and persisted.
    """
    from src.ingestion.loader import load_pdf

    api_key = os.environ.get("GEMINI_API_KEY", "")
    chunk_size = settings.get("pdf_parsing", {}).get("chunk_size_pages", 50)

    log.info("Phase 2: Parsing PDF %s (pdf_id=%d)", file_path, pdf_id)

    doc = load_pdf(file_path)
    pages = doc["pages"]
    all_tables = doc.get("tables", [])

    # Build a page→tables lookup
    tables_by_page: dict[int, list] = {}
    for tbl in all_tables:
        p = tbl["page_no"]
        tables_by_page.setdefault(p, []).append(tbl)

    # 2.4  Detect tag pattern from first 10 pages
    first_pages_text = "\n".join(p["text"] for p in pages[:10])
    tag_pattern = detect_tag_pattern(first_pages_text, api_key)

    # Collect all extracted items
    all_items: list[dict] = []
    seen_sigs: set[str] = set()

    for page in pages:
        page_no = page["page_no"]
        text = page["text"]
        page_tables = tables_by_page.get(page_no, [])

        # 2.2  Zone classification
        zone = classify_page_zone(text, page_tables, settings)

        # 2.3  Structured table extraction
        table_items = extract_table_rows(page_tables, zone, page_no)

        # 2.5  LLM chunk processing for GENERAL_TEXT or empty-table pages
        if zone == "GENERAL_TEXT" or (not table_items and text.strip()):
            llm_items = llm_extract_items(text, page_no, api_key)
        else:
            llm_items = []

        # 2.6  Technical spec extraction for DATASHEET zone
        page_specs = None
        if zone == "DATASHEET" and text.strip():
            page_specs = extract_technical_specs(text, api_key)

        # Merge table items and LLM items
        page_items = table_items + llm_items

        # Attach tech specs to items on this page if available
        if page_specs and page_items:
            for item in page_items:
                if not item.get("technical_spec"):
                    item["technical_spec"] = page_specs

        # Extract tag numbers from text and assign to nearby items
        tags_on_page = extract_tags_from_text(text, tag_pattern)
        if tags_on_page and page_items:
            for item in page_items:
                if not item.get("equipment_tag") and tags_on_page:
                    item["equipment_tag"] = tags_on_page[0]  # nearest tag

        # Deduplicate by (description + tag)
        for item in page_items:
            sig = (
                re.sub(r"\s+", " ", item["part_description"].lower().strip())
                + "_"
                + (item.get("equipment_tag") or "")
            )
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            all_items.append(item)

    # 2.7  Persist
    n_inserted = persist_items(lead_id, pdf_id, all_items)
    log.info(
        "Phase 2: PDF %s — %d pages, %d items extracted, %d persisted.",
        os.path.basename(file_path), len(pages), len(all_items), n_inserted,
    )
    return n_inserted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s"
    )

    parser = argparse.ArgumentParser(description="Phase 2: Parse PDF(s) for a lead")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--pdf-id", type=int, help="Parse a specific lead_pdfs row by ID")
    grp.add_argument("--lead-id", type=int, help="Parse all pending PDFs for a lead")
    args = parser.parse_args()

    settings = _load_settings()

    if args.pdf_id:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT lead_id, file_path FROM lead_pdfs WHERE id = %s",
                    (args.pdf_id,),
                )
                row = cur.fetchone()
        if not row:
            log.error("PDF ID %d not found.", args.pdf_id)
            sys.exit(1)
        parse_pdf(row[0], args.pdf_id, row[1], settings)

    else:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, file_path FROM lead_pdfs WHERE lead_id = %s AND parse_status = 'pending'",
                    (args.lead_id,),
                )
                rows = cur.fetchall()
        if not rows:
            log.warning("No pending PDFs found for lead_id=%d.", args.lead_id)
            sys.exit(0)
        for pdf_id, file_path in rows:
            parse_pdf(args.lead_id, pdf_id, file_path, settings)


if __name__ == "__main__":
    main()
