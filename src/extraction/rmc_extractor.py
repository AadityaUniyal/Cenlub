"""
src/extraction/rmc_extractor.py
================================
Rule-based extraction of RMC fields and spares tables from raw text.
All regex patterns live here — no LLM involved.
"""

from __future__ import annotations

import re
from typing import Dict, List

# ── Helpers ───────────────────────────────────────────────────────────────────


def _clean(v: str) -> str:
    """Strip whitespace, newlines, and trailing punctuation."""
    if not v:
        return ""
    return re.sub(r"\s+", " ", v.replace("\n", " ")).strip(" :;.,")


# ── Field patterns ────────────────────────────────────────────────────────────

_FIELD_PATTERNS: Dict[str, List[str]] = {
    "lead_no": [
        r"lead\s*(?:no|number)\s*[:\-]\s*(\w+)",
    ],
    "project_name": [
        r"project\s*(?:name)?\s*[:\-]\s*([^\n\r]+)",
    ],
    "customer": [
        r"customer\s*(?:name)?\s*[:\-]\s*([^\n\r]+)",
        r"purchasing\s+team\s*[-–]\s*([^\n\r]+)",
    ],
    "application": [
        r"application\s*[:\-]\s*([^\n\r]+)",
    ],
    "industry": [
        r"industry\s*[:\-]\s*([^\n\r]+)",
    ],
    "flow_rate": [
        r"flow\s*(?:rate)?\s*[:\-]\s*([\d\w\s\.\/]+)",
    ],
    "pressure": [
        r"operating\s+pressure\s*[:\-]\s*([\d\w\s\.\/]+)",
        r"pressure\s*[:\-]\s*([\d\w\s\.\/]+)",
    ],
    "temperature": [
        r"operating\s+temperature\s*[:\-]\s*([\d\w\s\.\/°]+)",
        r"temperature\s*[:\-]\s*([\d\w\s\.\/°]+)",
    ],
    "oil_grade": [
        r"oil\s*(?:grade)?\s*[:\-]\s*(ISO\s*VG\s*\d+\w*|[\d\w\s\-_]{2,20})",
    ],
    "filtration_level": [
        r"filtration\s*(?:level)?\s*[:\-]\s*([\d\w\s\.\/]+)",
    ],
    "heat_exchanger_moc": [
        r"heat\s+exchanger\s+moc[^:\-]*[:\-]\s*([\d\w\s\-_]+)",
        r"exchanger\s+moc\s*[:\-]\s*([\d\w\s\-_]+)",
    ],
    "reservoir_moc": [
        r"reservoir\s*(?:moc|material[^:\-]*)\s*[:\-]\s*([\d\w\s\-_]+)",
    ],
    "control_panel_requirement": [
        r"control\s+panel\s+req\w*\s*[:\-]\s*([^\n\r]+)",
        r"control\s+panel\s*[:\-]\s*([^\n\r]+)",
    ],
}

# Fields that are project metadata (use Email as highest priority source)
META_FIELDS = {"lead_no", "project_name", "customer", "industry", "application"}

# Priority ranking per source class (higher = more authoritative)
META_RANK = {
    "Email": 5, "LOS Specification": 4,
    "Technical Specification": 3, "BOM": 2, "Unknown": 1,
}
TECH_RANK = {
    "LOS Specification": 5, "Technical Specification": 4,
    "Email": 3, "BOM": 2, "Unknown": 1,
}


# ── Public API ────────────────────────────────────────────────────────────────

def extract_fields(text: str) -> Dict[str, str]:
    """
    Apply all regex patterns to *text* and return a dict of extracted values.
    Missing fields are returned as empty strings.
    """
    result: Dict[str, str] = {}
    for field, patterns in _FIELD_PATTERNS.items():
        result[field] = ""
        for pat in patterns:
            m = re.search(pat, text, re.I)
            if m:
                result[field] = _clean(m.group(1))
                break
    return result


def split_email_thread(text: str) -> List[str]:
    """
    Split an email thread PDF into individual messages.
    Segments are ordered newest-first (as they appear in the document).
    The pipeline reverses them so that newer values overwrite older ones.
    """
    delim = r"(-----\s*Original Message\s*-----|(?:^|\n)From:\s|\bOn\s.+?wrote:)"
    parts = re.split(delim, text, flags=re.I | re.DOTALL)
    segments, cur = [], []
    for part in parts:
        if part and re.match(delim, part, re.I | re.DOTALL):
            if cur:
                segments.append("".join(cur).strip())
                cur = []
        if part:
            cur.append(part)
    if cur:
        segments.append("".join(cur).strip())
    cleaned = [s for s in segments if len(s.strip()) > 30]
    return cleaned or [text]


def merge_extractions(all_extractions: List[Dict]) -> Dict[str, str]:
    """
    Merge field dicts from multiple documents using source-priority ranking.

    Each entry: {"classification": str, "fields": Dict[str, str]}
    Returns a single merged dict with the best value per field.
    """
    merged: Dict[str, str] = {k: "XX" for k in _FIELD_PATTERNS}
    source_cls: Dict[str, str] = {k: "Unknown" for k in _FIELD_PATTERNS}

    for entry in all_extractions:
        cls = entry["classification"]
        fields = entry["fields"]
        for key, val in fields.items():
            if not val or val == "XX" or key not in merged:
                continue
            ranks = META_RANK if key in META_FIELDS else TECH_RANK
            new_rank = ranks.get(cls, 1)
            old_rank = ranks.get(source_cls[key], 0)
            if merged[key] == "XX" or new_rank > old_rank:
                merged[key] = val
                source_cls[key] = cls

    return merged


def parse_spares_table(table: List[List[str]], spare_type: str) -> List[Dict]:
    """
    Parse a raw table (list-of-rows) into a list of spare dicts.
    spare_type: 'mandatory' | 'om'
    """
    if len(table) < 2:
        return []

    headers = [h.lower().strip() for h in table[0]]

    def _find(*keywords: str) -> int:
        for i, h in enumerate(headers):
            if any(k in h for k in keywords):
                return i
        return -1

    item_i = _find("item", "sr", "no")
    desc_i = _find("desc", "part name", "name")
    qty_i = _find("qty", "quantity", "nos")
    part_i = _find("part number", "part no")
    freq_i = _find("freq", "period", "recommended")

    # Fallback column assignments
    if item_i == -1:
        item_i = 0
    if desc_i == -1:
        desc_i = 1
    if qty_i == -1:
        qty_i = 2

    spares = []
    for row in table[1:]:
        if len(row) <= desc_i:
            continue
        desc = _clean(row[desc_i])
        if not desc or desc.lower() == "description":
            continue
        item = _clean(row[item_i]) if item_i < len(row) else ""
        qty = _clean(row[qty_i]) if qty_i >= 0 and qty_i < len(row) else "1"

        if spare_type == "mandatory":
            part = _clean(row[part_i]) if part_i >= 0 and part_i < len(row) else ""
            spares.append({"item_no": item, "description": desc, "qty": qty, "part_no": part})
        else:
            freq = _clean(row[freq_i]) if freq_i >= 0 and freq_i < len(row) else ""
            spares.append({"item_no": item, "description": desc, "qty": qty, "frequency": freq})

    return spares


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — EMAIL METADATA EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def extract_email_metadata(text: str) -> dict:
    """
    Extract structured metadata from an RFQ email body/PDF.
    Returns a dict matching EmailMetadata fields.
    """

    def _first(patterns, flags=re.I):
        for pat in patterns:
            m = re.search(pat, text, flags)
            if m:
                return _clean(m.group(1))
        return "XX"

    # Customer RFQ reference  e.g. C-4758
    customer_rfq_ref = _first([
        r"\bC-(\d{4,6})\b",
        r"rfq\s*(?:ref|reference|no\.?)\s*[:\-]\s*([\w\-]+)",
        r"inquiry\s*[/\\]\s*([\w\-]+)",
    ])
    if customer_rfq_ref != "XX" and not customer_rfq_ref.startswith("C-"):
        customer_rfq_ref = f"C-{customer_rfq_ref}" if customer_rfq_ref.isdigit() else customer_rfq_ref

    # Project name  e.g. FLOWMORE
    project_name = _first([
        r"(?:inquiry|rfq)\s*/\s*[\w\-]+\s*/\s*([\w\s]+?)\s*/",
        r"project\s*(?:name|tag)?\s*[:\-]\s*([^\n\r/]+)",
        r"subject:.*?/\s*([A-Z]{3,})\s*/",
    ])

    # Customer company
    customer_name = _first([
        r"from\s*:\s*[^\n]+\n.*?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\s+(?:Limited|Ltd|Pvt|Inc|Corp|Industries|Turbine|Power))",
        r"customer\s*[:\-]\s*([^\n\r]+)",
        r"purchasing\s+team\s*[-–]\s*([^\n\r]+)",
    ])

    # Contact person
    contact_person = _first([
        r"from\s*:\s*([A-Z][a-z]+\s+[A-Z][a-z]+)\s*[<\n]",
        r"regards[,\n\s]+([A-Z][a-z]+\s+[A-Z][a-z]+)",
        r"contact\s*[:\-]\s*([^\n\r]+)",
    ])

    # Contact email
    contact_email = _first([
        r"([\w.\-+]+@[\w.\-]+\.[a-zA-Z]{2,})",
    ])

    # Date received
    date_received = _first([
        r"(?:sent|date)\s*:\s*(\d{1,2}[-\s][A-Za-z]+[-\s]\d{2,4})",
        r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+20\d{2})",
        r"(\d{2}-\d{2}-20\d{2})",
    ])

    # CC list
    cc_match = re.search(r"cc\s*:\s*([^\n]+)", text, re.I)
    cc_list = _clean(cc_match.group(1)) if cc_match else ""

    # Attachments mentioned
    attachments = re.findall(r"[\w\s\-\.]+\.pdf", text, re.I)
    attachments = list(dict.fromkeys(a.strip() for a in attachments))  # deduplicate

    # Request action
    request_action = _first([
        r"(?:please|kindly)\s+(allocate[^\n.]+|submit[^\n.]+|provide[^\n.]+)",
    ])

    return {
        "customer_rfq_ref": customer_rfq_ref,
        "project_name": project_name,
        "customer_name": customer_name,
        "contact_person": contact_person,
        "contact_email": contact_email,
        "date_received": date_received,
        "cc_list": cc_list,
        "attachments": attachments,
        "request_action": request_action,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2A — SPARES TABLE WITH CATEGORY CODES & APPLICABILITY
# ══════════════════════════════════════════════════════════════════════════════

# Category header patterns from real O&M spares PDFs
_CATEGORY_PATTERNS = [
    (r"\b(A)\b.*?(centrifugal|pump)", "A", "Centrifugal Pumps"),
    (r"\b(B)\b.*?(motor|electrical)", "B", "Electrical Motor"),
    (r"\b(C)\b.*?(gear\s*box)", "C", "Gear Box"),
    (r"\b(D)\b.*?(steam\s*turbine|turbine)", "D", "Steam Turbine"),
]

_NOT_APPLICABLE_PATTERNS = [
    r"not\s+applicable\s+to\s+ttl",
    r"not\s+applicable",
    r"n/?a\b",
    r"not\s+required",
]

_MANDATORY_LINK_PATTERNS = [
    r"as\s+considered\s+for\s+mandatory",
    r"mandatory\s+spare",
    r"refer\s+mandatory",
]


def parse_spares_table_full(table: list, source_pdf: str,
                            lead_no: str = "", rfq_ref: str = "",
                            project: str = "", customer: str = "",
                            date: str = "") -> list:
    """
    Phase 2A extended parser — returns full BOMRow-compatible dicts
    including category_code, applicable_flag, mandatory_flag, qty_zero_flag.

    Works alongside the existing parse_spares_table() for backward compat.
    """
    if len(table) < 2:
        return []

    headers = [h.lower().strip() for h in table[0]]

    def _find(*kws):
        for i, h in enumerate(headers):
            if any(k in h for k in kws):
                return i
        return -1

    sl_i = _find("sl", "sr", "item", "no")
    desc_i = _find("part", "desc", "name", "item")
    qty_i = _find("qty", "quantity")
    unit_i = _find("unit")
    rem_i = _find("remark", "note", "comment")

    if sl_i == -1:
        sl_i = 0
    if desc_i == -1:
        desc_i = 1
    if qty_i == -1:
        qty_i = 2

    current_category_code = ""
    current_category_label = ""
    rows = []

    for row in table[1:]:
        if not any(c.strip() for c in row):
            continue

        # Detect category header rows (e.g. "A. Centrifugal Pumps")
        row_text = " ".join(str(c) for c in row if c).strip()
        detected = False
        for pat, code, label in _CATEGORY_PATTERNS:
            if re.search(pat, row_text, re.I):
                current_category_code = code
                current_category_label = label
                detected = True
                break
        if detected:
            continue

        # Also detect generic "A.", "B." etc. prefix headers
        if re.match(r'^[A-D]\s*[\.\)]\s*\w', row_text):
            code_m = re.match(r'^([A-D])', row_text)
            if code_m:
                current_category_code = code_m.group(1)
                current_category_label = row_text[:60].strip()
                continue

        if len(row) <= desc_i:
            continue

        desc = _clean(row[desc_i] if desc_i < len(row) else "")
        if not desc or desc.lower() in ("part description", "description", "item"):
            continue

        sl = _clean(row[sl_i] if sl_i < len(row) else "")
        qty = _clean(row[qty_i] if qty_i < len(row) else "")
        unit = _clean(row[unit_i] if unit_i >= 0 and unit_i < len(row) else "")
        rem = _clean(row[rem_i] if rem_i >= 0 and rem_i < len(row) else "")

        # Applicability flag
        not_applicable = any(re.search(p, rem, re.I) or re.search(p, desc, re.I)
                             for p in _NOT_APPLICABLE_PATTERNS)
        # Mandatory flag
        mandatory_linked = any(re.search(p, rem, re.I) for p in _MANDATORY_LINK_PATTERNS)

        # Qty zero flag
        qty_zero = (qty in ("0", "0.0", "Nil", "NIL", "") or
                    re.match(r"^0+$", qty or "0"))

        rows.append({
            "lead_no": lead_no,
            "customer_rfq_ref": rfq_ref,
            "project_name": project,
            "customer_name": customer,
            "date_received": date,
            "category_code": current_category_code,
            "category_label": current_category_label,
            "sl_no": sl,
            "item_description": desc,
            "plain_description": "",   # filled by LLM in Phase 2A.3
            "qty": qty,
            "unit": unit,
            "remarks": rem,
            "applicable_flag": not not_applicable,
            "mandatory_flag": mandatory_linked,
            "qty_zero_flag": bool(qty_zero),
            "data_complete_flag": bool(desc and qty and not qty_zero),
            "source_pdf": source_pdf,
            # Fields filled downstream
            "technical_specification": "",
            "moc": "",
            "cust_part_no": "",
            "rev": "",
            "assembly_group": "GENERAL",
            "internal_item_code": "",
            "make": "",
            "unit_price": "",
            "total_price": "",
            "delivery_weeks": "",
            "offer_ref": "",
            "validity_date": "",
        })

    return rows


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3.2 — INTERNAL ITEM CODE ASSIGNMENT
# ══════════════════════════════════════════════════════════════════════════════

_ITEM_CODE_RULES = [
    # (keyword_pattern, prefix, start_counter)
    (r"lube|oil|reservoir|filter|duplex", "L", {}),
    (r"motor|pump|mechanical|seal|bearing|rotor", "M", {}),
    (r"transmitter|sensor|gauge|indicator|pt|tt", "I", {}),
    (r"electrical|cable|junction|panel|switch", "E", {}),
    (r"pipe|valve|flange|skid|piping|line", "P", {}),
]

_item_counters: dict = {"L": 1, "M": 1, "I": 1, "E": 1, "P": 1}


def assign_item_codes(rows: list) -> list:
    """
    Phase 3.2: Assign internal Cenlub item codes (L-prefix, M-prefix, etc.)
    to each BOM row based on description keywords.
    Resets counters per pipeline call.
    """
    counters = {"L": 1, "M": 1, "I": 1, "E": 1, "P": 1}

    for row in rows:
        if row.get("internal_item_code"):
            continue
        desc = (row.get("item_description", "") + " " +
                row.get("category_label", "")).lower()
        prefix = "L"  # default
        for pat, pfx, _ in _ITEM_CODE_RULES:
            if re.search(pat, desc, re.I):
                prefix = pfx
                break
        code = f"{prefix}{counters[prefix]}"
        row["internal_item_code"] = code
        counters[prefix] += 1

    return rows


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3.3 — ASSEMBLY GROUP ASSIGNMENT
# ══════════════════════════════════════════════════════════════════════════════

_ASSEMBLY_RULES = [
    (r"reservoir|tank", "RESERVOIR ASSEMBLY"),
    (r"heat\s*exchanger|shell.*tube|cooler", "HEAT EXCHANGER ASSEMBLY"),
    (r"duplex.*filter|filter.*duplex|strainer", "DUPLEX FILTER ASSEMBLY"),
    (r"emergency.*pump|aux.*pump", "MOTOR PUMP ASSEMBLY (EMERGENCY OIL PUMP)"),
    (r"motor|pump|coupling", "LUBE OIL ASSEMBLY — MOTOR PUMP ASSEMBLY"),
    (r"water.*line|cooling.*line|waterline", "WATERLINE ASSEMBLY"),
    (r"header|delivery|manifold", "LUBE OIL DELIVERY HEADER"),
    (r"skid|frame|base|structure", "SKID ASSEMBLY"),
]


def assign_assembly_groups(rows: list) -> list:
    """Phase 3.3: Tag each BOM row with its Cenlub assembly group."""
    for row in rows:
        if row.get("assembly_group") and row["assembly_group"] != "GENERAL":
            continue
        desc = (row.get("item_description", "") + " " +
                row.get("category_label", "")).lower()
        for pat, group in _ASSEMBLY_RULES:
            if re.search(pat, desc, re.I):
                row["assembly_group"] = group
                break
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2D — MANDATORY CROSS-REFERENCE
# ══════════════════════════════════════════════════════════════════════════════

def cross_reference_mandatory(om_rows: list, mandatory_rows: list) -> list:
    """
    Phase 2D: For each O&M spare row, check if a matching item exists in the
    mandatory spares list. If yes, set mandatory_flag = True.
    Matching is done by normalised description substring.
    """
    # Build a set of normalised mandatory descriptions
    mand_descs = set()
    for m in mandatory_rows:
        d = m.get("description", m.get("item_description", "")).lower().strip()
        # Use first 4+ word tokens as key for fuzzy match
        tokens = re.sub(r"[^a-z0-9 ]", "", d).split()
        if len(tokens) >= 2:
            mand_descs.add(" ".join(tokens[:3]))

    for row in om_rows:
        if row.get("mandatory_flag"):
            continue
        d = row.get("item_description", "").lower().strip()
        tokens = re.sub(r"[^a-z0-9 ]", "", d).split()
        key = " ".join(tokens[:3])
        if key in mand_descs or any(key in md for md in mand_descs):
            row["mandatory_flag"] = True

    return om_rows


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2C — TECHNICAL SPECIFICATION → MOC PER ITEM
# ══════════════════════════════════════════════════════════════════════════════

# Keyword patterns used to match a BOM item description against spec text
_SPEC_WINDOW = 600   # characters around the matched keyword to extract as spec


def match_technical_specs(rows: list, tech_text: str) -> list:
    """
    Phase 2C: For each BOM row, search the Technical Part text for a passage
    that mentions the item's key descriptors. Populate:
      - technical_specification  (extracted passage, truncated to 300 chars)
      - moc                      (first MOC pattern found in that passage)

    Falls back gracefully (leaves fields empty) if nothing is found.
    """
    # Pre-compile MOC patterns
    moc_patterns = [
        (r"\bSS\s*304\b", "SS304"),
        (r"\bSS\s*316\b", "SS316"),
        (r"\bcarbon\s+steel\b", "Carbon Steel"),
        (r"\bmild\s+steel\b", "Carbon Steel"),
        (r"\bcast\s+iron\b", "Cast Iron"),
        (r"\bEN8\b", "EN8"),
        (r"\bEN24\b", "EN24"),
        (r"\bcopper\b", "Copper"),
        (r"\bbronze\b", "Bronze"),
        (r"\bbrass\b", "Brass"),
        (r"\bnibral\b", "NiBrAl"),
        (r"\bchrome\s+steel\b", "Chrome Steel"),
        (r"\bnitrile\b", "Nitrile"),
        (r"\bviton\b", "Viton"),
        (r"\bptfe\b", "PTFE"),
        (r"\bneoprene\b", "Neoprene"),
    ]

    for row in rows:
        desc = row.get("item_description", "")
        if not desc:
            continue

        # Build search keywords from first 3 non-trivial tokens
        tokens = re.sub(r"[^a-z0-9 ]", "", desc.lower()).split()
        keywords = [t for t in tokens if len(t) > 3][:3]
        if not keywords:
            continue

        # Find the earliest occurrence of any keyword in tech_text
        best_start = len(tech_text)
        for kw in keywords:
            m = re.search(re.escape(kw), tech_text, re.I)
            if m and m.start() < best_start:
                best_start = m.start()

        if best_start == len(tech_text):
            # No match found — leave fields as-is
            continue

        # Extract a window of text around the match
        snippet_start = max(0, best_start - 50)
        snippet_end = min(len(tech_text), best_start + _SPEC_WINDOW)
        snippet = tech_text[snippet_start:snippet_end].replace("\n", " ").strip()
        snippet = re.sub(r"\s{2,}", " ", snippet)

        if not row.get("technical_specification"):
            row["technical_specification"] = snippet[:300]

        # Extract MOC from the snippet if not already set
        if not row.get("moc") or row["moc"] == "XX":
            for pat, label in moc_patterns:
                if re.search(pat, snippet, re.I):
                    row["moc"] = label
                    break

    return rows
