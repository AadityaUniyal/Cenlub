"""
scripts/generate_csv.py
=======================
Phase 4 — Dynamic CSV generation.

Produces four CSV files per lead:
  FILE 1: BOM_{lead_no}_{project}_{customer}_{date}.csv       (full internal BOM)
  FILE 2: BOM_{lead_no}_VENDOR_INQUIRY.csv                    (vendor-ready)
  FILE 3: BOM_{lead_no}_UNMATCHED_REVIEW.csv                  (needs review)
  FILE 4: OA_MASTER_GAP_REPORT_{date}.csv                     (cross-lead gaps)

Column set is dynamically determined from what was found in the PDFs.
No column list is hardcoded.

Usage:
    python scripts/generate_csv.py --lead-id 5
    python scripts/generate_csv.py --lead-id 5 --out-dir outputs/csv/
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
from datetime import date
from typing import Any

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
# Helpers
# ---------------------------------------------------------------------------

def _slug(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"[^A-Za-z0-9]+", "_", s.strip()).strip("_")[:20]


def _normalize_qty(raw: str) -> int | None:
    if not raw:
        return None
    m = re.search(r"\b(\d+)\b", str(raw))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 4.1  Determine output columns dynamically
# ---------------------------------------------------------------------------

def _build_column_set(rows: list[dict]) -> list[str]:
    """
    Core columns are always present.
    Optional columns are added only if any row has a non-empty value for them.
    """
    core_cols = [
        "SR_NO", "EQUIPMENT_TAG", "EQUIPMENT_DESC",
        "OA_SNO", "OA_ITEM_NAME", "OA_CATEGORY",
        "PART_DESCRIPTION", "PLAIN_DESCRIPTION",
        "QTY_RAW", "QTY_NUMERIC", "UNIT", "REMARKS",
        "APPLICABLE_FLAG", "MANDATORY_FLAG",
        "MAKE", "UNIT_PRICE", "TOTAL_PRICE", "DELIVERY_WEEKS",
        "SOURCE_PAGE", "SOURCE_PDF",
        "MATCH_CONFIDENCE", "MATCH_METHOD", "REVIEW_FLAG",
        "DATA_COMPLETE_FLAG",
    ]

    optional_cols = {
        "MOC":                "moc",
        "TECHNICAL_SPEC":     "technical_spec_flat",
        "API_STANDARD":       "api_standard",
        "DRIVE_TYPE":         "drive_type",
        "AREA_CLASSIFICATION":"area_classification",
        "INTERNAL_ITEM_CODE": "internal_item_code",
        "OFFER_REF":          "offer_ref",
        "VALIDITY_DATE":      "validity_date",
    }

    present_optionals = []
    for col, field in optional_cols.items():
        if any(r.get(field) for r in rows):
            present_optionals.append(col)

    return core_cols + present_optionals


# ---------------------------------------------------------------------------
# 4.2  Data assembly from Neon DB
# ---------------------------------------------------------------------------

def assemble_rows(lead_id: int) -> list[dict]:
    """
    Query Neon DB and return all BOM rows for a lead as dicts.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    mi.id                    AS matched_id,
                    mi.match_confidence,
                    mi.match_method,
                    mi.plain_description,
                    mi.is_mandatory,
                    mi.is_applicable,
                    mi.review_flag,
                    ei.equipment_tag,
                    ei.equipment_desc,
                    ei.part_description,
                    ei.quantity_raw,
                    ei.quantity_numeric,
                    ei.unit,
                    ei.remarks,
                    ei.technical_spec,
                    ei.source_page,
                    lp.filename              AS source_pdf,
                    oi.sno                   AS oa_sno,
                    oi.item_name             AS oa_item_name,
                    oi.category              AS oa_category,
                    oi.item_code             AS internal_item_code,
                    bo.make,
                    bo.unit_price,
                    bo.total_price,
                    bo.delivery_weeks,
                    bo.offer_ref,
                    bo.validity_date
                FROM matched_items mi
                JOIN extracted_items ei      ON ei.id  = mi.extracted_item_id
                LEFT JOIN lead_pdfs lp       ON lp.id  = ei.pdf_id
                LEFT JOIN oa_master_items oi ON oi.id  = mi.oa_item_id
                LEFT JOIN bom_output bo      ON bo.matched_item_id = mi.id
                WHERE ei.lead_id = %s
                ORDER BY ei.equipment_tag NULLS LAST,
                         oi.category NULLS LAST,
                         oi.sno NULLS LAST,
                         ei.id
                """,
                (lead_id,),
            )
            rows = cur.fetchall()
            colnames = [d[0] for d in cur.description]

    result = []
    for i, row in enumerate(rows, 1):
        d = dict(zip(colnames, row))
        d["sr_no"] = i

        # 4.3  Quantity normalization
        if d.get("quantity_numeric") is None and d.get("quantity_raw"):
            d["quantity_numeric"] = _normalize_qty(d["quantity_raw"])

        # 4.4  Total price calculation
        if d.get("unit_price") and d.get("quantity_numeric"):
            try:
                d["total_price"] = float(d["unit_price"]) * int(d["quantity_numeric"])
            except (TypeError, ValueError):
                pass

        # Flatten technical_spec JSONB to string
        ts = d.get("technical_spec")
        if ts:
            if isinstance(ts, dict):
                d["technical_spec_flat"] = "; ".join(f"{k}: {v}" for k, v in ts.items())
            elif isinstance(ts, str):
                d["technical_spec_flat"] = ts
            else:
                d["technical_spec_flat"] = str(ts)
        else:
            d["technical_spec_flat"] = ""

        # MOC — try to extract from technical_spec
        if not d.get("moc"):
            ts_dict = d.get("technical_spec") or {}
            if isinstance(ts_dict, dict):
                for key in ("moc", "material", "material_of_construction"):
                    if ts_dict.get(key):
                        d["moc"] = str(ts_dict[key])
                        break

        # Detect optional fields from technical_spec
        if isinstance(ts, dict):
            for key, field in [
                ("api_standard", "api_standard"),
                ("drive_type", "drive_type"),
                ("area_classification", "area_classification"),
            ]:
                val = ts.get(key) or ts.get(key.replace("_", " "))
                if val:
                    d[field] = str(val)

        # data_complete flag
        d["data_complete"] = bool(
            d.get("part_description") and
            d.get("quantity_numeric") and
            d.get("match_confidence") != "UNMATCHED"
        )

        result.append(d)

    return result


# ---------------------------------------------------------------------------
# Lead metadata fetch
# ---------------------------------------------------------------------------

def _fetch_lead_meta(lead_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lead_no, project_name, customer_name, date_received FROM leads WHERE id = %s",
                (lead_id,),
            )
            row = cur.fetchone()
    if not row:
        return {}
    return {
        "lead_no": row[0] or "",
        "project_name": row[1] or "",
        "customer_name": row[2] or "",
        "date_received": str(row[3]) if row[3] else "",
    }


# ---------------------------------------------------------------------------
# CSV writer helper
# ---------------------------------------------------------------------------

def _write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info("  Written: %s (%d rows)", path, len(rows))


# ---------------------------------------------------------------------------
# Row → CSV dict mapper
# ---------------------------------------------------------------------------

def _map_row(row: dict, sr_no: int) -> dict:
    return {
        "SR_NO":               sr_no,
        "EQUIPMENT_TAG":       row.get("equipment_tag") or "",
        "EQUIPMENT_DESC":      row.get("equipment_desc") or "",
        "OA_SNO":              row.get("oa_sno") or "",
        "OA_ITEM_NAME":        row.get("oa_item_name") or "",
        "OA_CATEGORY":         row.get("oa_category") or "",
        "PART_DESCRIPTION":    row.get("part_description") or "",
        "PLAIN_DESCRIPTION":   row.get("plain_description") or "",
        "QTY_RAW":             row.get("quantity_raw") or "",
        "QTY_NUMERIC":         row.get("quantity_numeric") or "",
        "UNIT":                row.get("unit") or "",
        "REMARKS":             row.get("remarks") or "",
        "APPLICABLE_FLAG":     "YES" if row.get("is_applicable", True) else "NO",
        "MANDATORY_FLAG":      "YES" if row.get("is_mandatory", False) else "NO",
        "MAKE":                row.get("make") or "",
        "UNIT_PRICE":          row.get("unit_price") or "",
        "TOTAL_PRICE":         row.get("total_price") or "",
        "DELIVERY_WEEKS":      row.get("delivery_weeks") or "",
        "SOURCE_PAGE":         row.get("source_page") or "",
        "SOURCE_PDF":          row.get("source_pdf") or "",
        "MATCH_CONFIDENCE":    row.get("match_confidence") or "",
        "MATCH_METHOD":        row.get("match_method") or "",
        "REVIEW_FLAG":         "YES" if row.get("review_flag", False) else "NO",
        "DATA_COMPLETE_FLAG":  "YES" if row.get("data_complete", False) else "NO",
        # Optional
        "MOC":                 row.get("moc") or "",
        "TECHNICAL_SPEC":      row.get("technical_spec_flat") or "",
        "API_STANDARD":        row.get("api_standard") or "",
        "DRIVE_TYPE":          row.get("drive_type") or "",
        "AREA_CLASSIFICATION": row.get("area_classification") or "",
        "INTERNAL_ITEM_CODE":  row.get("internal_item_code") or "",
        "OFFER_REF":           row.get("offer_ref") or "",
        "VALIDITY_DATE":       str(row.get("validity_date") or ""),
    }


# ---------------------------------------------------------------------------
# 4.5  Generate all four CSV files
# ---------------------------------------------------------------------------

def generate_csvs(lead_id: int, out_dir: str) -> dict[str, str]:
    """
    Generate all 4 CSV files for a lead. Returns dict of label → file path.
    """
    meta = _fetch_lead_meta(lead_id)
    lead_no     = meta.get("lead_no", f"LEAD{lead_id}")
    project     = _slug(meta.get("project_name", ""))
    customer    = _slug(meta.get("customer_name", ""))
    date_str    = re.sub(r"[^A-Za-z0-9]", "", meta.get("date_received", ""))[:9] or \
                  date.today().strftime("%d%b%Y")

    lead_dir = os.path.join(out_dir, lead_no)
    os.makedirs(lead_dir, exist_ok=True)

    rows = assemble_rows(lead_id)
    if not rows:
        log.warning("No matched items found for lead_id=%d.", lead_id)
        return {}

    # Map to CSV dicts
    csv_rows = [_map_row(r, i + 1) for i, r in enumerate(rows)]

    # 4.1 Dynamic column set
    all_cols = _build_column_set(csv_rows)

    # ── FILE 1: Full internal BOM ─────────────────────────────────────────────
    f1_name = "_".join(filter(None, ["BOM", lead_no, project, customer, date_str])) + ".csv"
    f1_path = os.path.join(lead_dir, f1_name)
    _write_csv(f1_path, all_cols, csv_rows)

    # ── FILE 2: Vendor Inquiry (HIGH + PROBABLE only) ─────────────────────────
    vendor_cols = [
        "SR_NO", "PART_DESCRIPTION", "PLAIN_DESCRIPTION",
        "QTY_NUMERIC", "UNIT", "TECHNICAL_SPEC", "MOC",
        "MAKE", "UNIT_PRICE", "DELIVERY_WEEKS",
    ]
    vendor_rows = [
        r for r in csv_rows
        if r["MATCH_CONFIDENCE"] in ("HIGH", "PROBABLE")
    ]
    # Re-number
    for i, r in enumerate(vendor_rows, 1):
        r["SR_NO"] = i

    f2_name = f"BOM_{lead_no}_VENDOR_INQUIRY.csv"
    f2_path = os.path.join(lead_dir, f2_name)
    _write_csv(f2_path, vendor_cols, vendor_rows)

    # ── FILE 3: Unmatched Review ──────────────────────────────────────────────
    unmatched_cols = [
        "SR_NO", "PART_DESCRIPTION", "EQUIPMENT_TAG",
        "QTY_NUMERIC", "SOURCE_PAGE", "SOURCE_PDF",
        "MATCH_CONFIDENCE", "REVIEW_FLAG",
    ]
    unmatched_rows = [
        r for r in csv_rows
        if r["MATCH_CONFIDENCE"] == "UNMATCHED" or r["REVIEW_FLAG"] == "YES"
    ]
    for i, r in enumerate(unmatched_rows, 1):
        r["SR_NO"] = i

    f3_name = f"BOM_{lead_no}_UNMATCHED_REVIEW.csv"
    f3_path = os.path.join(lead_dir, f3_name)
    _write_csv(f3_path, unmatched_cols, unmatched_rows)

    # ── FILE 4: OA Master Gap Report (cross-lead) ─────────────────────────────
    gap_cols = [
        "SR_NO", "PART_DESCRIPTION", "FREQUENCY",
        "SUGGESTED_NAME", "SUGGESTED_CATEGORY", "STATUS",
    ]
    gap_rows_raw = _fetch_gap_report()
    gap_rows_csv = [
        {
            "SR_NO":              i + 1,
            "PART_DESCRIPTION":   g["part_description"],
            "FREQUENCY":          g["frequency"],
            "SUGGESTED_NAME":     g["suggested_name"] or "",
            "SUGGESTED_CATEGORY": g["suggested_category"] or "",
            "STATUS":             g["status"],
        }
        for i, g in enumerate(gap_rows_raw)
    ]
    today_str = date.today().strftime("%d%b%Y")
    f4_name = f"OA_MASTER_GAP_REPORT_{today_str}.csv"
    f4_path = os.path.join(lead_dir, f4_name)
    _write_csv(f4_path, gap_cols, gap_rows_csv)

    # Update lead status
    _update_lead_status(lead_id, "bom_ready")

    return {
        "bom_full":         f1_path,
        "vendor_inquiry":   f2_path,
        "unmatched_review": f3_path,
        "gap_report":       f4_path,
    }


def _fetch_gap_report() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT part_description, frequency, suggested_name,
                       suggested_category, status
                FROM oa_master_gaps
                ORDER BY frequency DESC, created_at DESC
                LIMIT 500
                """
            )
            rows = cur.fetchall()
    return [
        {
            "part_description":   r[0],
            "frequency":          r[1],
            "suggested_name":     r[2],
            "suggested_category": r[3],
            "status":             r[4],
        }
        for r in rows
    ]


def _update_lead_status(lead_id: int, status: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE leads SET status = %s WHERE id = %s",
                (status, lead_id),
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s"
    )

    parser = argparse.ArgumentParser(description="Phase 4: Generate BOM CSVs")
    parser.add_argument("--lead-id", type=int, required=True)
    parser.add_argument("--out-dir", default="outputs/csv/",
                        help="Base output directory (default: outputs/csv/)")
    args = parser.parse_args()

    settings = _load_settings()
    out_dir = args.out_dir or settings.get("output", {}).get("base_path", "outputs/csv/")

    files = generate_csvs(args.lead_id, out_dir)
    print("\n── Phase 4 Output Files ──────────────────────────────────")
    for label, path in files.items():
        print(f"  {label:<20}: {path}")
    print("──────────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
