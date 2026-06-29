"""
src/extraction/bom_extractor.py
================================
BOM (Bill of Materials) extraction with three-tier fallback:
  1. Groq  (Llama 3.1-8b via instructor)
  2. Gemini 2.5 Flash
  3. Rule-based pipe-delimited parser

All three return a BOMStructure instance so the caller never needs to
know which extractor was used.
"""

from __future__ import annotations

import re
import time
import logging
from typing import List, Optional

from src.core.models import BOMComponent, BOMEquipment, BOMStructure

log = logging.getLogger(__name__)


# ── Tag / price helpers ───────────────────────────────────────────────────────

def split_tags(tag: str) -> List[str]:
    """
    Expand combined equipment tags into individual tags.
      P-101A/B      →  ['P-101A', 'P-101B']
      XV-101/102    →  ['XV-101', 'XV-102']
      P-101A/B/C    →  ['P-101A', 'P-101B', 'P-101C']
    """
    if not tag:
        return []
    tag = tag.strip()

    # Letter variants: P-101A/B/C
    m = re.match(r"^([A-Za-z]+-\d+)([A-Za-z])((?:/[A-Za-z])+)(.*)$", tag)
    if m:
        base, first, rest, extra = m.group(1), m.group(2), m.group(3), m.group(4)
        return [f"{base}{s.strip()}{extra}" for s in [first] + rest.strip("/").split("/")]

    # Number variants: XV-101/102/103
    m = re.match(r"^([A-Za-z]+-)(\d+)((?:/\d+)+)(.*)$", tag)
    if m:
        prefix, first, rest, extra = m.group(1), m.group(2), m.group(3), m.group(4)
        return [f"{prefix}{n.strip()}{extra}" for n in [first] + rest.strip("/").split("/")]

    if "/" in tag:
        return [p.strip() for p in tag.split("/") if p.strip()]
    return [tag]


def clean_price(raw: str) -> str:
    """
    Reconstruct scrambled currency strings.
    e.g. 'otIoNrRs 75,000'  →  'INR 75,000'
    """
    if not raw:
        return ""
    raw = raw.strip()
    m = re.search(r"[\d,.]+", raw)
    if not m:
        return raw
    number = m.group(0)
    low = raw.lower()
    if "$" in raw or "usd" in low:
        currency = "USD"
    elif "eur" in low or "€" in raw:
        currency = "EUR"
    else:
        currency = "INR"
    return f"{currency} {number}"


# ── Groq extractor ────────────────────────────────────────────────────────────

def _extract_with_groq(content: str, groq_key: str) -> Optional[BOMStructure]:
    try:
        import instructor
        from openai import OpenAI

        client = instructor.from_openai(
            OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key)
        )
        prompt = (
            "Extract a structured BOM from this raw table data.\n"
            "Clean scrambled text (e.g. 'otIoNrRs 75,000' → 'INR 75,000').\n"
            "Identify main equipments, sub-components, tags, quantities, materials and prices.\n\n"
            f"Raw data:\n---\n{content}\n---"
        )
        time.sleep(2)   # avoid rate-limit on free tier
        return client.chat.completions.create(
            model="llama-3.1-8b-instant",
            response_model=BOMStructure,
            messages=[
                {"role": "system",
                 "content": "You are a precise industrial BOM extraction assistant."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
    except Exception as exc:
        log.warning(f"Groq BOM extraction failed: {exc}")
        return None


# ── Gemini extractor ──────────────────────────────────────────────────────────

def _extract_with_gemini(content: str, gemini_key: str) -> Optional[BOMStructure]:
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=gemini_key)
        prompt = (
            "Extract a structured BOM from this raw table data.\n"
            "Clean scrambled text, identify equipments, sub-components, "
            "tags, quantities, materials and prices.\n\n"
            f"Raw data:\n{content}"
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=BOMStructure,
                temperature=0.0,
            ),
        )
        if resp.text:
            return BOMStructure.model_validate_json(resp.text)
    except Exception as exc:
        log.warning(f"Gemini BOM extraction failed: {exc}")
    return None


# ── Rule-based extractor ──────────────────────────────────────────────────────

def _extract_rule_based(content: str) -> BOMStructure:
    """
    Simple pipe-delimited parser for BOM tables.
    Handles lines like:  1 | Screw Pump | 2 Nos | Cast Iron | INR 75,000
    """
    log.info("Using rule-based BOM parser.")
    equipments: List[BOMEquipment] = []

    for line in content.splitlines():
        if "|" not in line or "item no" in line.lower():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue

        item_no = parts[0]
        desc = parts[1]
        qty = parts[2]
        mat = parts[3] if len(parts) > 3 else ""
        price = clean_price(parts[4]) if len(parts) > 4 else ""

        components: List[BOMComponent] = []
        if mat and ":" in mat:
            for seg in mat.split(","):
                if ":" in seg:
                    name_part, mat_part = seg.split(":", 1)
                    components.append(
                        BOMComponent(name=name_part.strip(), material=mat_part.strip())
                    )

        equipments.append(
            BOMEquipment(
                item_no=item_no,
                description=desc,
                qty=qty,
                material_specification=mat,
                unit_price=price,
                equipment_tag="",
                components=components,
            )
        )

    return BOMStructure(project_name="Rule-based", equipment_list=equipments)


# ── Tables → text helper ──────────────────────────────────────────────────────

def tables_to_text(tables: list) -> str:
    """Convert list-of-table dicts into a readable text block for the LLM."""
    lines = []
    for t in tables:
        lines.append(f"--- Table (page {t['page_no']}) ---")
        for row in t["data"]:
            lines.append(" | ".join(row))
        lines.append("")
    return "\n".join(lines)


# ── Main entry point ──────────────────────────────────────────────────────────

def extract_bom(doc: dict, groq_key: str = "", gemini_key: str = "") -> Optional[BOMStructure]:
    """
    Extract BOM structure from an ingested PDF document dict.
    Tries Groq → Gemini → rule-based in that order.
    Returns None if the document has no recognisable BOM content.
    """
    content = tables_to_text(doc.get("tables", [])) or doc.get("full_text", "")
    if len(content.strip()) < 20:
        return None

    if groq_key:
        result = _extract_with_groq(content, groq_key)
        if result:
            log.info("BOM extracted via Groq.")
            return result

    if gemini_key:
        result = _extract_with_gemini(content, gemini_key)
        if result:
            log.info("BOM extracted via Gemini.")
            return result

    return _extract_rule_based(content)
