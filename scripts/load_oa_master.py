"""
scripts/load_oa_master.py
=========================
Phase 0 — One-time loader: reads the OA Master Excel file into Neon DB.

Usage:
    python scripts/load_oa_master.py --file path/to/OA_Master.xlsx

Re-running is safe — existing items are skipped (no duplicates inserted).
Synonyms are auto-generated via Gemini for each new item.

Requires: DATABASE_URL and GEMINI_API_KEY environment variables.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.db import execute_schema, get_conn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("load_oa_master")


# ---------------------------------------------------------------------------
# Excel parsing
# ---------------------------------------------------------------------------

def parse_oa_master_excel(path: str) -> list[dict]:
    """
    Dynamically parse any OA Master Excel file.
    Detects category header rows (rows with only one non-empty cell in the
    item-name column) and assigns them as the category for subsequent rows.

    Returns a list of dicts:
      {sno, item_name, category, item_code, type_flag, sheet_source}
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    items = []

    for sheet in wb.worksheets:
        sheet_name = sheet.title
        log.info("  Parsing sheet: %s", sheet_name)

        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue

        # Detect header row — find the row with "item" or "description" in cells
        header_idx = 0
        col_sno = col_name = col_code = col_flag = None

        for i, row in enumerate(rows[:10]):
            cells = [str(c).lower().strip() if c else "" for c in row]
            if any("item" in c or "description" in c or "name" in c for c in cells):
                header_idx = i
                for j, c in enumerate(cells):
                    if ("sno" in c or "s.no" in c or "sr" in c) and col_sno is None:
                        col_sno = j
                    if ("name" in c or "item" in c or "description" in c) and col_name is None:
                        col_name = j
                    if "code" in c and col_code is None:
                        col_code = j
                    if ("type" in c or "flag" in c) and col_flag is None:
                        col_flag = j
                break

        # Fallbacks
        if col_name is None:
            col_name = 1
        if col_sno is None:
            col_sno = 0

        current_category = sheet_name
        data_rows = rows[header_idx + 1:]

        for row in data_rows:
            if not any(c for c in row if c is not None):
                continue

            # Detect category header: row where only the name column has a value
            non_empty = [j for j, c in enumerate(row) if c is not None and str(c).strip()]
            if len(non_empty) == 1 and non_empty[0] == col_name:
                current_category = str(row[col_name]).strip()
                continue

            name_val = row[col_name] if col_name < len(row) else None
            if not name_val or str(name_val).strip() in ("", "Item Name", "Description"):
                continue

            item_name = str(name_val).strip()
            sno_val = row[col_sno] if col_sno < len(row) else None
            code_val = row[col_code] if col_code is not None and col_code < len(row) else None
            flag_val = row[col_flag] if col_flag is not None and col_flag < len(row) else None

            items.append({
                "sno": float(sno_val) if sno_val is not None else None,
                "item_name": item_name,
                "category": current_category,
                "item_code": str(code_val).strip() if code_val else None,
                "type_flag": str(flag_val).strip() if flag_val else None,
                "sheet_source": sheet_name,
            })

    log.info("  Parsed %d items across %d sheets.", len(items), len(wb.worksheets))
    return items


# ---------------------------------------------------------------------------
# Synonym generation via Gemini
# ---------------------------------------------------------------------------

def generate_synonyms_gemini(item_name: str, api_key: str) -> list[str]:
    """
    Ask Gemini to generate 5–10 alternate names / abbreviations for an
    industrial equipment item. Returns a list of strings.
    Falls back to empty list if Gemini is unavailable.
    """
    if not api_key:
        return []
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        prompt = (
            f"Generate 5 to 10 alternate names and common abbreviations for "
            f"this industrial equipment item that might appear in a technical "
            f"PDF or BOM document: '{item_name}'. "
            f"Return a JSON array of strings only. No explanation."
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.3,
            ),
        )
        if resp.text:
            result = json.loads(resp.text)
            if isinstance(result, list):
                return [str(s).strip() for s in result if s]
    except Exception as exc:
        log.warning("Synonym generation failed for '%s': %s", item_name, exc)
    return []


# ---------------------------------------------------------------------------
# Database insert
# ---------------------------------------------------------------------------

def load_items_to_db(items: list[dict], gemini_key: str) -> tuple[int, int]:
    """
    Insert items and synonyms into Neon DB.
    Skips items whose (item_name, category) already exist.
    Returns (items_inserted, synonyms_inserted).
    """
    items_inserted = 0
    synonyms_inserted = 0

    with get_conn() as conn:
        with conn.cursor() as cur:
            for item in items:
                # Check for duplicate
                cur.execute(
                    "SELECT id FROM oa_master_items WHERE item_name = %s AND category = %s",
                    (item["item_name"], item["category"]),
                )
                row = cur.fetchone()
                if row:
                    log.debug("  Skipping existing: %s / %s", item["item_name"], item["category"])
                    continue

                # Insert item
                cur.execute(
                    """
                    INSERT INTO oa_master_items
                        (sno, item_name, category, item_code, type_flag, sheet_source)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        item["sno"], item["item_name"], item["category"],
                        item["item_code"], item["type_flag"], item["sheet_source"],
                    ),
                )
                item_id = cur.fetchone()[0]
                items_inserted += 1

                # Generate and insert synonyms
                synonyms = generate_synonyms_gemini(item["item_name"], gemini_key)
                # Always include the item_name itself as a synonym
                all_synonyms = list({item["item_name"].lower(), *[s.lower() for s in synonyms]})

                for syn in all_synonyms:
                    if not syn:
                        continue
                    cur.execute(
                        """
                        INSERT INTO oa_master_synonyms (oa_item_id, synonym, added_by)
                        VALUES (%s, %s, 'system')
                        ON CONFLICT DO NOTHING
                        """,
                        (item_id, syn),
                    )
                    synonyms_inserted += 1

                log.info(
                    "  Loaded: [%s] %s → %d synonyms",
                    item["category"], item["item_name"], len(all_synonyms),
                )

    return items_inserted, synonyms_inserted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 0: Load OA Master Excel into Neon PostgreSQL"
    )
    parser.add_argument(
        "--file", required=True,
        help="Path to OA Master Excel file (.xlsx)",
    )
    parser.add_argument(
        "--schema", default="scripts/schema.sql",
        help="Path to schema.sql (default: scripts/schema.sql)",
    )
    args = parser.parse_args()

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        log.warning("GEMINI_API_KEY not set — synonyms will not be generated.")

    # Ensure tables exist
    log.info("Applying schema...")
    execute_schema(args.schema)

    # Parse Excel
    log.info("Parsing OA Master: %s", args.file)
    items = parse_oa_master_excel(args.file)

    if not items:
        log.error("No items found in Excel file. Check column structure.")
        sys.exit(1)

    # Load to DB
    log.info("Loading %d items to Neon DB...", len(items))
    n_items, n_synonyms = load_items_to_db(items, gemini_key)

    print(f"\nLoaded {n_items} items, {n_synonyms} synonyms into Neon database.")
    print(f"(Skipped items that already existed.)")


if __name__ == "__main__":
    main()
