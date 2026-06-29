-- =============================================================================
-- schema.sql  — Neon PostgreSQL schema for Cenlub RFQ-to-BOM pipeline
-- Run once: psql $DATABASE_URL -f scripts/schema.sql
-- =============================================================================

-- Enable fuzzy matching extension (needed for Phase 5 dedup)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Phase 0: OA Master reference tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS oa_master_items (
    id           SERIAL PRIMARY KEY,
    sno          NUMERIC,
    item_name    TEXT NOT NULL,
    category     TEXT NOT NULL,
    item_code    TEXT,
    type_flag    TEXT,            -- BN / FT / FTNC / NC / STOCK / BN STOCK / NA
    sheet_source TEXT,
    created_at   TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oa_master_synonyms (
    id          SERIAL PRIMARY KEY,
    oa_item_id  INTEGER REFERENCES oa_master_items(id) ON DELETE CASCADE,
    synonym     TEXT NOT NULL,
    added_by    TEXT DEFAULT 'system',  -- 'system' or 'manual'
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_synonyms_item ON oa_master_synonyms(oa_item_id);
CREATE INDEX IF NOT EXISTS idx_synonyms_text ON oa_master_synonyms USING gin(synonym gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- Phase 1: Lead tracking
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS leads (
    id               SERIAL PRIMARY KEY,
    lead_no          TEXT UNIQUE NOT NULL,
    customer_rfq_ref TEXT,
    project_name     TEXT,
    customer_name    TEXT,
    contact_person   TEXT,
    contact_email    TEXT,
    date_received    DATE,
    status           TEXT DEFAULT 'received',  -- received / parsed / bom_ready / quoted
    email_subject    TEXT,
    email_body       TEXT,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lead_pdfs (
    id           SERIAL PRIMARY KEY,
    lead_id      INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    filename     TEXT,
    pdf_type     TEXT,          -- LARGE_SPEC / SPARES_LIST / MANDATORY_SPARES / BOM / OTHER
    page_count   INTEGER,
    file_path    TEXT,
    parse_status TEXT DEFAULT 'pending',  -- pending / in_progress / complete / failed
    created_at   TIMESTAMPTZ DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Phase 2: Extracted items (raw, from PDF parsing)
-- ---------------------------------------------------------------------------

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
    source_zone       TEXT,     -- LINE_ITEM / DATASHEET / SPARES / OM_SPARES / GENERAL_TEXT
    is_duplicate      BOOLEAN DEFAULT false,
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_extracted_lead ON extracted_items(lead_id);
CREATE INDEX IF NOT EXISTS idx_extracted_tag  ON extracted_items(equipment_tag);
CREATE INDEX IF NOT EXISTS idx_extracted_desc ON extracted_items USING gin(part_description gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- Phase 3: OA Master matching results
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS matched_items (
    id                  SERIAL PRIMARY KEY,
    extracted_item_id   INTEGER REFERENCES extracted_items(id) ON DELETE CASCADE,
    oa_item_id          INTEGER REFERENCES oa_master_items(id),  -- NULL = unmatched
    match_confidence    TEXT,    -- HIGH / PROBABLE / LLM / UNMATCHED
    match_method        TEXT,    -- KEYWORD / FUZZY / LLM / NONE
    match_score         NUMERIC, -- fuzzy score 0-100 (NULL for KEYWORD/LLM)
    plain_description   TEXT,
    is_mandatory        BOOLEAN DEFAULT false,
    is_applicable       BOOLEAN DEFAULT true,
    review_flag         BOOLEAN DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_matched_extracted ON matched_items(extracted_item_id);
CREATE INDEX IF NOT EXISTS idx_matched_oa_item   ON matched_items(oa_item_id);

-- ---------------------------------------------------------------------------
-- Phase 4: BOM output (vendor fills: make, price, delivery)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bom_output (
    id                  SERIAL PRIMARY KEY,
    lead_id             INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    matched_item_id     INTEGER REFERENCES matched_items(id),
    sr_no               INTEGER,
    make                TEXT,
    unit_price          NUMERIC,
    total_price         NUMERIC,     -- auto-calculated: qty_numeric * unit_price
    delivery_weeks      INTEGER,
    offer_ref           TEXT,
    validity_date       DATE,
    internal_item_code  TEXT,
    data_complete       BOOLEAN DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Gap tracking (cross-lead, persistent)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS oa_master_gaps (
    id                   SERIAL PRIMARY KEY,
    part_description     TEXT NOT NULL,
    frequency            INTEGER DEFAULT 1,
    suggested_name       TEXT,
    suggested_category   TEXT,
    status               TEXT DEFAULT 'pending',  -- pending / added / rejected
    created_at           TIMESTAMPTZ DEFAULT now(),
    UNIQUE(part_description)
);
