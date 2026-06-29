import re
import logging

logger = logging.getLogger(__name__)


def clean_value(val: str) -> str:
    """Cleans whitespace, newlines, and trailing punctuation from a value."""
    if not val:
        return ""
    val = val.replace("\n", " ")
    val = re.sub(r'\s+', ' ', val)
    return val.strip(" :;.,")


def split_email_thread(text: str) -> list:
    """
    Phase 6: Splits a raw email text thread into chronological
    message blocks. Splits only on message-starting delimiters
    (like 'From:' or 'Original Message') to keep single message
    header fields (Sent, Subject, To) grouped together.
    """
    # Split only on tokens that denote the beginning of a new email
    headers_pattern = (
        r"(-----Original Message-----|\bFrom:|\bOn\s+.*,\s+.*wrote:)"
    )
    parts = re.split(headers_pattern, text)

    segments = []
    current_segment = []

    for part in parts:
        if part:
            # Check if this part matches our thread starter pattern
            if re.match(headers_pattern, part):
                if current_segment:
                    segments.append("".join(current_segment).strip())
                    current_segment = []
            current_segment.append(part)

    if current_segment:
        segments.append("".join(current_segment).strip())

    # Filter out empty or tiny noise segments
    cleaned_segments = [s for s in segments if len(s.strip()) > 30]

    if not cleaned_segments:
        return [text]

    return cleaned_segments


def extract_fields_from_text(text: str) -> dict:
    """
    Extracts predefined fields from raw text using regular expressions.
    """
    extracted = {}

    # Regex patterns for key engineering and project values
    patterns = {
        "lead_no": [
            r"lead\s*(?:no|number)\s*[:\-]\s*(\w+)",
            r"lead\s*[:\-]\s*(\w+)"
        ],
        "project_name": [
            r"project\s*(?:name)?\s*[:\-]\s*([^\n\r]+)",
            r"project\s*[:\-]\s*([^\n\r]+)"
        ],
        "customer": [
            r"customer\s*(?:name)?\s*[:\-]\s*([^\n\r]+)",
            r"purchasing\s+team\s*-\s*([^\n\r]+)"
        ],
        "application": [
            r"application\s*[:\-]\s*([^\n\r]+)"
        ],
        "industry": [
            r"industry\s*[:\-]\s*([^\n\r]+)"
        ],
        "flow_rate": [
            r"flow\s*(?:rate)?\s*[:\-]\s*([\d\w\s\.\/]+)",
            r"flow\s*[:\-]\s*([\d\w\s\.\/]+)"
        ],
        "pressure": [
            r"operating\s+pressure\s*[:\-]\s*([\d\w\s\.\/]+)",
            r"pressure\s*[:\-]\s*([\d\w\s\.\/]+)"
        ],
        "temperature": [
            r"operating\s+temperature\s*[:\-]\s*"
            r"([\d\w\s\.\/\°\d]+)",
            r"temperature\s*[:\-]\s*([\d\w\s\.\/\°\d]+)"
        ],
        "oil_grade": [
            r"oil\s*(?:grade)?\s*[:\-]\s*([\d\w\s\-\_]+)"
        ],
        "filtration_level": [
            r"filtration\s*(?:level)?\s*[:\-]\s*([\d\w\s\.\/]+)"
        ],
        "heat_exchanger_moc": [
            r"heat\s+exchanger\s+moc\s*(?:\([^)]+\))?\s*[:\-]\s*"
            r"([\d\w\s\-\_]+)",
            r"exchanger\s+moc\s*[:\-]\s*([\d\w\s\-\_]+)"
        ],
        "reservoir_moc": [
            r"reservoir\s*(?:material\s+of\s+construction|moc)\s*"
            r"(?:\([^)]+\))?\s*[:\-]\s*([\d\w\s\-\_]+)",
            r"reservoir\s+moc\s*[:\-]\s*([\d\w\s\-\_]+)"
        ],
        "control_panel_requirement": [
            r"control\s+panel\s+(?:requirements?|req)\s*[:\-]\s*"
            r"([^\n\r]+)",
            r"control\s+panel\s*[:\-]\s*([^\n\r]+)"
        ]
    }

    for field, regex_list in patterns.items():
        extracted[field] = ""
        for regex in regex_list:
            match = re.search(regex, text, re.IGNORECASE)
            if match:
                extracted[field] = clean_value(match.group(1))
                break

    return extracted


def parse_spares_table(table_data: list, spare_type: str) -> list:
    """
    Parses a spares list table. Maps columns dynamically based on headers.
    """
    if not table_data or len(table_data) < 2:
        return []

    headers = [str(h).lower().strip() for h in table_data[0]]
    rows = table_data[1:]

    item_idx = -1
    desc_idx = -1
    qty_idx = -1
    part_idx = -1
    freq_idx = -1

    for idx, h in enumerate(headers):
        if any(term in h for term in ["item", "sr", "s.", "no"]):
            item_idx = idx
        elif any(term in h for term in [
                "description", "part name", "name", "desc"]):
            desc_idx = idx
        elif any(term in h for term in ["qty", "quantity", "nos"]):
            qty_idx = idx
        elif any(term in h for term in [
                "part number", "part no", "part"]):
            part_idx = idx
        elif any(term in h for term in [
                "freq", "frequency", "recommended", "period"]):
            freq_idx = idx

    if desc_idx == -1 and len(headers) > 1:
        desc_idx = 1
    if qty_idx == -1 and len(headers) > 2:
        qty_idx = 2
    if item_idx == -1:
        item_idx = 0

    spares = []
    for row in rows:
        if len(row) > max(item_idx, desc_idx):
            desc_val = clean_value(row[desc_idx])
            if not desc_val or desc_val.lower() == "description":
                continue

            item_val = clean_value(
                row[item_idx]) if item_idx < len(row) else ""
            qty_val = clean_value(
                row[qty_idx]
            ) if qty_idx >= 0 and qty_idx < len(row) else "1"

            if spare_type == "mandatory":
                part_val = clean_value(
                    row[part_idx]
                ) if part_idx >= 0 and part_idx < len(row) else ""
                spares.append({
                    "item_no": item_val,
                    "description": desc_val,
                    "qty": qty_val,
                    "part_no": part_val
                })
            else:
                freq_val = clean_value(
                    row[freq_idx]
                ) if freq_idx >= 0 and freq_idx < len(row) else ""
                spares.append({
                    "item_no": item_val,
                    "description": desc_val,
                    "qty": qty_val,
                    "frequency": freq_val
                })

    return spares
