"""
scripts/multi_pdf.py
====================
Phase 5 — Multi-PDF parallel processing orchestrator.

For a given lead_id, spins up one worker per PDF using
multiprocessing.Pool, waits for all to complete, then runs
cross-PDF deduplication and reconciliation.

Usage:
    python scripts/multi_pdf.py --lead-id 5
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.db import get_conn

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Worker function (runs in a child process)
# ---------------------------------------------------------------------------

def _worker(args: tuple) -> dict:
    """Parse one PDF. Designed to run inside multiprocessing.Pool."""
    lead_id, pdf_id, file_path, settings = args

    import logging as _log
    _log.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Dynamic import inside worker to avoid pickling issues
    from scripts.parse_pdf import parse_pdf

    try:
        n = parse_pdf(lead_id, pdf_id, file_path, settings)
        return {"pdf_id": pdf_id, "items_extracted": n, "status": "ok"}
    except Exception as exc:
        return {"pdf_id": pdf_id, "items_extracted": 0, "status": f"error: {exc}"}


# ---------------------------------------------------------------------------
# 5.2  Deduplication via pg_trgm
# ---------------------------------------------------------------------------

def dedup_extracted_items(lead_id: int, similarity_threshold: float = 0.9) -> int:
    """
    Mark duplicate extracted_items rows using pg_trgm similarity.
    Two rows are duplicates if their part_description similarity > threshold
    AND they share the same equipment_tag (or both have no tag).
    Returns count of duplicates marked.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extracted_items ei_dup
                SET is_duplicate = true
                FROM extracted_items ei_keep
                WHERE ei_dup.lead_id = %s
                  AND ei_keep.lead_id = %s
                  AND ei_dup.id > ei_keep.id
                  AND ei_dup.is_duplicate = false
                  AND COALESCE(ei_dup.equipment_tag, '') = COALESCE(ei_keep.equipment_tag, '')
                  AND similarity(
                        lower(ei_dup.part_description),
                        lower(ei_keep.part_description)
                      ) > %s
                """,
                (lead_id, lead_id, similarity_threshold),
            )
            count = cur.rowcount
    log.info("Phase 5.2: Marked %d duplicate items for lead_id=%d.", count, lead_id)
    return count


# ---------------------------------------------------------------------------
# 5.3  Cross-PDF reconciliation
# ---------------------------------------------------------------------------

def reconcile_cross_pdf(lead_id: int) -> None:
    """
    For each equipment tag found across multiple PDFs:
    - Link LINE_ITEM items with SPARES items from other PDFs.
    - Flag tags that have LINE_ITEM entries but zero spares.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Find tags with LINE_ITEM entries but no SPARES/OM_SPARES entries
            cur.execute(
                """
                SELECT DISTINCT ei.equipment_tag
                FROM extracted_items ei
                WHERE ei.lead_id = %s
                  AND ei.source_zone = 'LINE_ITEM_LIST'
                  AND ei.is_duplicate = false
                  AND ei.equipment_tag IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM extracted_items ei2
                      WHERE ei2.lead_id = %s
                        AND ei2.equipment_tag = ei.equipment_tag
                        AND ei2.source_zone IN ('MANDATORY_SPARES', 'OM_SPARES')
                        AND ei2.is_duplicate = false
                  )
                """,
                (lead_id, lead_id),
            )
            no_spares_tags = [r[0] for r in cur.fetchall()]

        if no_spares_tags:
            log.info(
                "Phase 5.3: %d equipment tag(s) have no spare parts in PDFs: %s",
                len(no_spares_tags),
                ", ".join(no_spares_tags[:10]),
            )


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def process_all_pdfs_for_lead(lead_id: int, settings: dict) -> dict:
    """
    Phase 5 main function: parallel PDF parsing + dedup + reconciliation.
    """
    # Fetch all pending PDFs for this lead
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, file_path FROM lead_pdfs WHERE lead_id = %s AND parse_status = 'pending'",
                (lead_id,),
            )
            pdfs = cur.fetchall()

    if not pdfs:
        log.warning("No pending PDFs for lead_id=%d.", lead_id)
        return {"total_pdfs": 0, "total_items": 0}

    log.info("Phase 5.1: Processing %d PDF(s) for lead_id=%d...", len(pdfs), lead_id)

    # Build worker args
    worker_args = [(lead_id, pdf_id, file_path, settings) for pdf_id, file_path in pdfs]

    # Run in parallel (cap at CPU count or number of PDFs)
    n_workers = min(len(pdfs), multiprocessing.cpu_count())
    if n_workers > 1:
        with multiprocessing.Pool(processes=n_workers) as pool:
            results = pool.map(_worker, worker_args)
    else:
        results = [_worker(a) for a in worker_args]

    total_items = sum(r["items_extracted"] for r in results)
    errors = [r for r in results if r["status"].startswith("error")]
    if errors:
        for e in errors:
            log.error("PDF id=%d failed: %s", e["pdf_id"], e["status"])

    # 5.2  Deduplication
    sim_thresh = settings.get("matching", {}).get("dedup_similarity_threshold", 90) / 100.0
    dups = dedup_extracted_items(lead_id, sim_thresh)

    # 5.3  Cross-PDF reconciliation
    reconcile_cross_pdf(lead_id)

    return {
        "total_pdfs":    len(pdfs),
        "total_items":   total_items,
        "duplicates":    dups,
        "errors":        len(errors),
        "worker_results": results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s"
    )

    parser = argparse.ArgumentParser(description="Phase 5: Multi-PDF parallel processing")
    parser.add_argument("--lead-id", type=int, required=True)
    args = parser.parse_args()

    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "settings.json")
    with open(path, encoding="utf-8") as fh:
        settings = json.load(fh)

    result = process_all_pdfs_for_lead(args.lead_id, settings)
    print(f"\nPhase 5 complete: {result['total_pdfs']} PDFs, "
          f"{result['total_items']} items extracted, "
          f"{result['duplicates']} duplicates removed, "
          f"{result['errors']} errors.")


if __name__ == "__main__":
    main()
