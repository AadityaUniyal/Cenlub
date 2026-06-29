"""
scripts/seed_oa_master_csv.py
==============================
Reads "Copy of OA Master - OCS - Ordering Tracker.csv" and inserts every
item into oa_master_items, grouped by its correct category heading.

The CSV structure:
  - Category rows have no Sno. value (empty first cell) but have a name
    in the LIST OF ITEMS column, e.g. "Main Items", "Valves", etc.
  - Item rows have a numeric Sno. and a name.
  - Columns: Sno. | LIST OF ITEMS | BN/FT/... | Item Code | ...

Usage:
    python scripts/seed_oa_master_csv.py

Requires: DATABASE_URL in environment (or .env file).
Re-running is safe — duplicate (item_name, category) pairs are skipped.
"""

from __future__ import annotations

import csv
import logging
import os
import sys

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("seed_oa_master")

CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "Copy of OA Master - OCS - Ordering Tracker.csv",
)

# ── Schema (idempotent) ───────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS oa_master_items (
    id           SERIAL PRIMARY KEY,
    sno          NUMERIC,
    item_name    TEXT NOT NULL,
    category     TEXT NOT NULL,
    item_code    TEXT,
    type_flag    TEXT,
    sheet_source TEXT,
    created_at   TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oa_master_synonyms (
    id          SERIAL PRIMARY KEY,
    oa_item_id  INTEGER REFERENCES oa_master_items(id) ON DELETE CASCADE,
    synonym     TEXT NOT NULL,
    added_by    TEXT DEFAULT 'system',
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS leads (
    id               SERIAL PRIMARY KEY,
    lead_no          TEXT UNIQUE NOT NULL,
    customer_rfq_ref TEXT,
    project_name     TEXT,
    customer_name    TEXT,
    contact_person   TEXT,
    contact_email    TEXT,
    date_received    DATE,
    status           TEXT DEFAULT 'received',
    email_subject    TEXT,
    email_body       TEXT,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lead_pdfs (
    id           SERIAL PRIMARY KEY,
    lead_id      INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    filename     TEXT,
    pdf_type     TEXT,
    page_count   INTEGER,
    file_path    TEXT,
    parse_status TEXT DEFAULT 'pending',
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS extracted_items (
    id                SERIAL PRIMARY KEY,
    lead_id           INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    pdf_id            INTEGER REFERENCES lead_pdfs(id),
    equipment_tag     TEXT,
    equipment_desc    TEXT,
    part_description  TEXT NOT NULL,
    quantity_raw      TEXT,
    quantity_numeric  INTEGER,
    unit              TEXT,
    remarks           TEXT,
    technical_spec    JSONB,
    source_page       INTEGER,
    source_zone       TEXT,
    is_duplicate      BOOLEAN DEFAULT false,
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS matched_items (
    id                  SERIAL PRIMARY KEY,
    extracted_item_id   INTEGER REFERENCES extracted_items(id) ON DELETE CASCADE,
    oa_item_id          INTEGER REFERENCES oa_master_items(id),
    match_confidence    TEXT,
    match_method        TEXT,
    match_score         NUMERIC,
    plain_description   TEXT,
    is_mandatory        BOOLEAN DEFAULT false,
    is_applicable       BOOLEAN DEFAULT true,
    review_flag         BOOLEAN DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bom_output (
    id                  SERIAL PRIMARY KEY,
    lead_id             INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    matched_item_id     INTEGER REFERENCES matched_items(id),
    sr_no               INTEGER,
    make                TEXT,
    unit_price          NUMERIC,
    total_price         NUMERIC,
    delivery_weeks      INTEGER,
    offer_ref           TEXT,
    validity_date       DATE,
    internal_item_code  TEXT,
    data_complete       BOOLEAN DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oa_master_gaps (
    id                   SERIAL PRIMARY KEY,
    part_description     TEXT NOT NULL,
    frequency            INTEGER DEFAULT 1,
    suggested_name       TEXT,
    suggested_category   TEXT,
    status               TEXT DEFAULT 'pending',
    created_at           TIMESTAMPTZ DEFAULT now(),
    UNIQUE(part_description)
);
"""

# ── CSV parser ────────────────────────────────────────────────────────────────

def parse_csv(path: str) -> list[dict]:
    """
    Parse the OA Master CSV.
    Returns list of dicts: {sno, item_name, category, type_flag, item_code}
    """
    items = []
    current_category = "Uncategorized"

    # Known category header names (rows where sno is blank)
    CATEGORY_NAMES = {
        "main items", "control valves", "valves", "transmitters",
        "switches", "gauges", "instruments fittings", "electrical items",
        "tank & accessories", "finishing items",
    }

    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        rows = list(reader)

    # Find the actual data start — the row with "Sno." header
    data_start = 0
    for i, row in enumerate(rows):
        if row and str(row[0]).strip().lower() == "sno.":
            data_start = i + 1
            break

    for row in rows[data_start:]:
        if len(row) < 2:
            continue

        sno_raw   = str(row[0]).strip()
        name_raw  = str(row[1]).strip() if len(row) > 1 else ""
        flag_raw  = str(row[2]).strip() if len(row) > 2 else ""
        code_raw  = str(row[3]).strip() if len(row) > 3 else ""

        if not name_raw:
            continue

        # Detect category header: no sno, and name matches known category
        if not sno_raw and name_raw.lower() in CATEGORY_NAMES:
            current_category = name_raw
            log.info("  Category: %s", current_category)
            continue

        # Also treat any row with no numeric sno but a populated name as a
        # potential category header (covers unlisted category names)
        if not sno_raw:
            # Could be metadata rows at top (Lead No:, Customer: etc.) — skip those
            skip_keywords = [
                "lead no", "customer", "oa number", "sno", "list of items",
                "ocs ordering tracker", "vendor name", "pr no", "po no",
                "purchase", "engineering", "sales", "inquiry to vendor",
            ]
            if any(k in name_raw.lower() for k in skip_keywords):
                continue
            # Otherwise treat as a category header
            current_category = name_raw
            log.info("  Category: %s", current_category)
            continue

        # Must have a numeric sno to be a real item
        try:
            sno = float(sno_raw)
        except ValueError:
            continue

        item_name = name_raw.strip()
        if not item_name:
            continue

        items.append({
            "sno":       sno,
            "item_name": item_name,
            "category":  current_category,
            "type_flag": flag_raw or None,
            "item_code": code_raw or None,
        })

    log.info("Parsed %d items from CSV.", len(items))
    return items


# ── DB insert ─────────────────────────────────────────────────────────────────

def seed_database(items: list[dict]) -> tuple[int, int]:
    """
    Apply schema (idempotent) then insert all items.
    Returns (inserted, skipped).
    """
    import psycopg2

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise EnvironmentError(
            "DATABASE_URL is not set. Add it to your .env file."
        )

    conn = psycopg2.connect(dsn)
    inserted = skipped = 0

    try:
        with conn.cursor() as cur:
            log.info("Applying schema (CREATE TABLE IF NOT EXISTS)...")
            cur.execute(SCHEMA_SQL)
        conn.commit()
        log.info("Schema ready.")

        with conn.cursor() as cur:
            # Fetch all existing (item_name, category) pairs in one query
            cur.execute("SELECT item_name, category FROM oa_master_items")
            existing = {(r[0], r[1]) for r in cur.fetchall()}

            synonym_pairs = []   # (item_id, synonym)

            for item in items:
                key = (item["item_name"], item["category"])
                if key in existing:
                    skipped += 1
                    continue

                cur.execute(
                    """
                    INSERT INTO oa_master_items
                        (sno, item_name, category, type_flag, item_code, sheet_source)
                    VALUES (%s, %s, %s, %s, %s, 'OA Master CSV')
                    RETURNING id
                    """,
                    (
                        item["sno"],
                        item["item_name"],
                        item["category"],
                        item["type_flag"],
                        item["item_code"],
                    ),
                )
                item_id = cur.fetchone()[0]
                synonym_pairs.append((item_id, item["item_name"].lower()))
                inserted += 1

            # Bulk insert synonyms
            if synonym_pairs:
                cur.executemany(
                    """
                    INSERT INTO oa_master_synonyms (oa_item_id, synonym, added_by)
                    VALUES (%s, %s, 'system')
                    ON CONFLICT DO NOTHING
                    """,
                    synonym_pairs,
                )

        conn.commit()

    finally:
        conn.close()

    return inserted, skipped


# ── Report ─────────────────────────────────────────────────────────────────────

def print_summary(items: list[dict]) -> None:
    """Print a grouped summary of what will be / was inserted."""
    from collections import Counter
    counts = Counter(i["category"] for i in items)
    print("\n── Items by Category ─────────────────────────────────────")
    for cat, n in counts.most_common():
        print(f"  {cat:<35} {n:>3} items")
    print(f"  {'TOTAL':<35} {sum(counts.values()):>3} items")
    print("──────────────────────────────────────────────────────────\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if not os.path.exists(CSV_PATH):
        log.error("CSV not found: %s", CSV_PATH)
        sys.exit(1)

    log.info("Parsing: %s", os.path.basename(CSV_PATH))
    items = parse_csv(CSV_PATH)

    if not items:
        log.error("No items parsed. Check CSV format.")
        sys.exit(1)

    print_summary(items)

    log.info("Connecting to Neon DB and seeding...")
    inserted, skipped = seed_database(items)

    print(f"Done. Inserted: {inserted}  |  Skipped (already existed): {skipped}")


if __name__ == "__main__":
    main()
