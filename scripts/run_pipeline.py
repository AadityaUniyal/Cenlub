"""
scripts/run_pipeline.py
=======================
Master runner — calls all phases in order for a single RFQ.

Usage:
    # Full pipeline from email + PDFs
    python scripts/run_pipeline.py --email data/raw/rfq_email.pdf \\
        --pdfs data/raw/bom_sheet.pdf data/raw/om_spares.pdf

    # Start from an existing lead (already in DB from Phase 1)
    python scripts/run_pipeline.py --lead-id 5

    # Skip Phase 3 OA matching (useful if OA Master not loaded yet)
    python scripts/run_pipeline.py --lead-id 5 --skip-matching

Environment variables required:
    DATABASE_URL   — Neon PostgreSQL connection string
    GEMINI_API_KEY — Google Gemini API key
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("run_pipeline")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "settings.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Pipeline phases
# ---------------------------------------------------------------------------

def run_pipeline(
    email_path: str = "",
    pdf_paths: list[str] | None = None,
    lead_id: int | None = None,
    skip_matching: bool = False,
    out_dir: str = "",
) -> dict:
    """
    Full Phase 1–4 (+ 5 for multi-PDF) pipeline runner.
    Returns a summary dict with lead info and output file paths.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    settings = _load_settings()
    out_dir = out_dir or settings.get("output", {}).get("base_path", "outputs/csv/")

    # Setup logging to file
    log_dir = settings.get("logging", {}).get("log_dir", "logs/")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"pipeline_{ts}.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
    logging.getLogger().addHandler(fh)

    summary: dict = {}

    # ── PHASE 1: Email intake (if not resuming from existing lead) ────────────
    if lead_id is None:
        log.info("=" * 60)
        log.info("PHASE 1 — Email intake & lead creation")
        log.info("=" * 60)

        if not email_path and not pdf_paths:
            # Try default demo data
            email_path = os.path.join("data", "raw", "rfq_email.pdf")
            raw_dir = os.path.join("data", "raw")
            pdf_paths = [
                os.path.join(raw_dir, f)
                for f in sorted(os.listdir(raw_dir))
                if f.lower().endswith(".pdf") and f != "rfq_email.pdf"
            ] if os.path.isdir(raw_dir) else []

        email_text = ""
        if email_path and os.path.exists(email_path):
            if email_path.lower().endswith(".pdf"):
                from src.ingestion.loader import load_pdf
                doc = load_pdf(email_path)
                email_text = doc["full_text"]
            else:
                with open(email_path, encoding="utf-8", errors="replace") as fh_:
                    email_text = fh_.read()

        from scripts.parse_email import process_email
        phase1 = process_email(email_text, pdf_paths or [], gemini_key, settings)
        lead_id = phase1["lead_id"]
        lead_no = phase1["lead_no"]
        summary["lead_no"] = lead_no
        summary["lead_id"] = lead_id
        summary["phase1"] = {
            "customer": phase1["meta"].get("customer_name"),
            "project":  phase1["meta"].get("project_name"),
            "pdfs":     len(phase1["pdfs"]),
        }
        log.info("Phase 1 complete: Lead %s (id=%d)", lead_no, lead_id)
    else:
        summary["lead_id"] = lead_id
        log.info("Resuming pipeline for existing lead_id=%d", lead_id)

    # ── PHASE 5 / 2: Parse all PDFs (parallel if multiple) ────────────────────
    log.info("=" * 60)
    log.info("PHASE 2/5 — PDF parsing (parallel)")
    log.info("=" * 60)

    from scripts.multi_pdf import process_all_pdfs_for_lead
    phase2 = process_all_pdfs_for_lead(lead_id, settings)
    summary["phase2"] = phase2
    log.info(
        "Phase 2/5 complete: %d items extracted, %d duplicates removed.",
        phase2.get("total_items", 0),
        phase2.get("duplicates", 0),
    )

    # ── PHASE 3: OA Master matching ────────────────────────────────────────────
    if not skip_matching:
        log.info("=" * 60)
        log.info("PHASE 3 — OA Master matching")
        log.info("=" * 60)

        from scripts.match_items import match_items_for_lead
        phase3 = match_items_for_lead(lead_id, settings)
        summary["phase3"] = phase3
        log.info("Phase 3 complete: %s", phase3)
    else:
        log.info("Phase 3 skipped (--skip-matching flag).")
        summary["phase3"] = {"skipped": True}

    # ── PHASE 4: CSV generation ────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("PHASE 4 — CSV generation")
    log.info("=" * 60)

    from scripts.generate_csv import generate_csvs
    files = generate_csvs(lead_id, out_dir)
    summary["phase4"] = files
    log.info("Phase 4 complete: %d files generated.", len(files))

    # ── Summary ────────────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("PIPELINE COMPLETE")
    log.info("=" * 60)

    summary["log_file"] = log_path
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cenlub RFQ → BOM CSV Pipeline (v4 — Neon DB backed)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--email", default="",
        help="Path to RFQ email file (.eml or .pdf)",
    )
    parser.add_argument(
        "--pdfs", nargs="*", default=[],
        help="Paths to PDF attachments",
    )
    parser.add_argument(
        "--lead-id", type=int, default=None,
        help="Resume from existing lead ID (skips Phase 1)",
    )
    parser.add_argument(
        "--skip-matching", action="store_true",
        help="Skip Phase 3 OA Master matching",
    )
    parser.add_argument(
        "--out-dir", default="",
        help="Output CSV directory (default from settings.json)",
    )
    parser.add_argument(
        "--setup-schema", action="store_true",
        help="Run schema.sql to create DB tables, then exit",
    )
    args = parser.parse_args()

    if args.setup_schema:
        from scripts.db import execute_schema
        execute_schema()
        print("Schema applied successfully.")
        return

    result = run_pipeline(
        email_path=args.email,
        pdf_paths=args.pdfs,
        lead_id=args.lead_id,
        skip_matching=args.skip_matching,
        out_dir=args.out_dir,
    )

    print("\n══ Pipeline Summary ══════════════════════════════════════")
    print(f"  Lead No. / ID  : {result.get('lead_no', 'N/A')} / {result.get('lead_id')}")
    p1 = result.get("phase1", {})
    if p1:
        print(f"  Customer       : {p1.get('customer')}")
        print(f"  Project        : {p1.get('project')}")
        print(f"  PDFs registered: {p1.get('pdfs')}")
    p2 = result.get("phase2", {})
    print(f"  Items extracted: {p2.get('total_items', 0)}")
    print(f"  Duplicates rm'd: {p2.get('duplicates', 0)}")
    p3 = result.get("phase3", {})
    if not p3.get("skipped"):
        print(f"  Match results  : HIGH={p3.get('HIGH',0)} PROBABLE={p3.get('PROBABLE',0)} "
              f"LLM={p3.get('LLM',0)} UNMATCHED={p3.get('UNMATCHED',0)}")
    files = result.get("phase4", {})
    print("\n  Output files:")
    for label, path in files.items():
        print(f"    {label:<20}: {path}")
    print(f"\n  Log            : {result.get('log_file')}")
    print("══════════════════════════════════════════════════════════\n")


if __name__ == "__main__":
    main()
