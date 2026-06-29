"""
scripts/match_items.py
======================
Phase 3 — OA Master matching engine (3-tier: keyword → fuzzy → LLM).

For every extracted_item belonging to a lead, finds the best match in
oa_master_items. Results are written to the matched_items table.
Gap items are tracked in oa_master_gaps.

Usage:
    python scripts/match_items.py --lead-id 5
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
# 3.1  Load OA Master reference at runtime
# ---------------------------------------------------------------------------

def load_oa_master() -> list[dict]:
    """
    Fetch all OA Master items with their synonyms from Neon DB.
    Returns a list of dicts:
      {id, sno, item_name, category, item_code, synonyms: [str, ...]}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    i.id, i.sno, i.item_name, i.category, i.item_code,
                    COALESCE(array_agg(s.synonym) FILTER (WHERE s.synonym IS NOT NULL), '{}') AS synonyms
                FROM oa_master_items i
                LEFT JOIN oa_master_synonyms s ON s.oa_item_id = i.id
                GROUP BY i.id, i.sno, i.item_name, i.category, i.item_code
                ORDER BY i.id
                """
            )
            rows = cur.fetchall()

    items = []
    for row in rows:
        items.append({
            "id":        row[0],
            "sno":       row[1],
            "item_name": row[2],
            "category":  row[3],
            "item_code": row[4],
            "synonyms":  list(row[5]) if row[5] else [],
        })
    log.info("Loaded %d OA Master items from DB.", len(items))
    return items


# ---------------------------------------------------------------------------
# Text normalizer
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


# ---------------------------------------------------------------------------
# 3.2  Tier 1 — Keyword / synonym match
# ---------------------------------------------------------------------------

def _keyword_match(description: str, oa_items: list[dict]) -> Optional[dict]:
    """
    Check if any OA Master synonym is contained in the description.
    Returns the best (longest synonym) match, or None.
    """
    norm_desc = _normalize(description)
    best_item = None
    best_len = 0

    for item in oa_items:
        for syn in item["synonyms"]:
            norm_syn = _normalize(syn)
            if len(norm_syn) < 3:
                continue
            if norm_syn in norm_desc and len(norm_syn) > best_len:
                best_item = item
                best_len = len(norm_syn)

    return best_item


# ---------------------------------------------------------------------------
# 3.3  Tier 2 — Fuzzy match
# ---------------------------------------------------------------------------

def _fuzzy_match(description: str, oa_items: list[dict],
                 high_threshold: int, review_threshold: int) -> Optional[tuple[dict, float]]:
    """
    Run rapidfuzz token_sort_ratio against OA Master item names.
    Returns (best_item, score) or None if below review_threshold.
    """
    try:
        from rapidfuzz import fuzz  # type: ignore
    except ImportError:
        log.warning("rapidfuzz not installed — Tier 2 fuzzy matching skipped.")
        return None

    norm_desc = _normalize(description)
    best_item = None
    best_score = 0.0

    for item in oa_items:
        score = fuzz.token_sort_ratio(norm_desc, _normalize(item["item_name"]))
        if score > best_score:
            best_score = score
            best_item = item

    if best_score >= review_threshold:
        return best_item, best_score
    return None


# ---------------------------------------------------------------------------
# 3.4  Tier 3 — LLM classification
# ---------------------------------------------------------------------------

def _llm_match(description: str, oa_items: list[dict], api_key: str) -> Optional[tuple[dict, str]]:
    """
    Ask Gemini to classify the description against OA Master item names.
    Returns (matched_item, suggested_category) or None if NO MATCH.
    """
    if not api_key:
        return None

    item_names = [item["item_name"] for item in oa_items]
    items_list = "\n".join(f"- {n}" for n in item_names[:200])  # limit to 200 items

    prompt = (
        f"You are classifying industrial equipment spare parts.\n"
        f"Given this part description: '{description}'\n\n"
        f"Which of the following standard items does it best match?\n"
        f"{items_list}\n\n"
        f"Return a JSON object with:\n"
        f'  {{"matched_item": "<exact item name from the list or NO MATCH>", '
        f'"suggested_category": "<category if NO MATCH>", '
        f'"reasoning": "<one sentence>"}}'
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
            matched_name = result.get("matched_item", "").strip()
            suggested_cat = result.get("suggested_category", "")

            if matched_name and matched_name != "NO MATCH":
                for item in oa_items:
                    if item["item_name"].strip().lower() == matched_name.lower():
                        return item, suggested_cat
            return None
    except Exception as exc:
        log.warning("LLM match failed for '%s': %s", description[:60], exc)
    return None


# ---------------------------------------------------------------------------
# 3.6  Plain description generation
# ---------------------------------------------------------------------------

def generate_plain_description(
    part_description: str, equipment_desc: str, api_key: str
) -> str:
    """
    Write a 1–2 sentence plain-language description via Gemini.
    Returns empty string if unavailable.
    """
    if not api_key:
        return ""
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            "Write a 1–2 sentence plain-language description of what this "
            "industrial spare part is and why it is needed. Use simple language "
            "a non-engineer can understand.\n"
            f"Part: '{part_description}'. Context: '{equipment_desc or 'industrial equipment'}'."
        )
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return (resp.text or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 3.5  Gap tracking
# ---------------------------------------------------------------------------

def record_gap(description: str, suggested_name: str, suggested_category: str) -> None:
    """Insert or increment frequency in oa_master_gaps."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oa_master_gaps (part_description, suggested_name, suggested_category, frequency)
                VALUES (%s, %s, %s, 1)
                ON CONFLICT (part_description)
                DO UPDATE SET frequency = oa_master_gaps.frequency + 1
                """,
                (description[:500], suggested_name[:200] if suggested_name else None,
                 suggested_category[:200] if suggested_category else None),
            )


# ---------------------------------------------------------------------------
# 3.7  Insert matched_items row
# ---------------------------------------------------------------------------

def insert_matched_item(
    extracted_item_id: int,
    oa_item_id: Optional[int],
    confidence: str,
    method: str,
    score: Optional[float],
    plain_description: str,
    is_mandatory: bool,
    is_applicable: bool,
    review_flag: bool,
) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO matched_items
                    (extracted_item_id, oa_item_id, match_confidence, match_method,
                     match_score, plain_description, is_mandatory, is_applicable, review_flag)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    extracted_item_id, oa_item_id, confidence, method,
                    score, plain_description, is_mandatory, is_applicable, review_flag,
                ),
            )
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Main matching function
# ---------------------------------------------------------------------------

def match_items_for_lead(lead_id: int, settings: dict) -> dict:
    """
    Run the 3-tier matching engine for all extracted items in a lead.
    Returns a summary dict.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    m_cfg = settings.get("matching", {})
    high_thresh = m_cfg.get("fuzzy_high_threshold", 75)
    review_thresh = m_cfg.get("fuzzy_review_threshold", 60)

    # 3.1  Load OA Master
    oa_items = load_oa_master()
    if not oa_items:
        log.warning("OA Master is empty — all items will be UNMATCHED. "
                    "Run load_oa_master.py first.")

    # Fetch all extracted items for this lead that haven't been matched yet
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ei.id, ei.part_description, ei.equipment_desc,
                       ei.quantity_raw, ei.quantity_numeric
                FROM extracted_items ei
                WHERE ei.lead_id = %s AND ei.is_duplicate = false
                AND ei.id NOT IN (SELECT extracted_item_id FROM matched_items)
                ORDER BY ei.id
                """,
                (lead_id,),
            )
            items = cur.fetchall()

    log.info("Phase 3: Matching %d extracted items for lead_id=%d", len(items), lead_id)

    counts = {"HIGH": 0, "PROBABLE": 0, "LLM": 0, "UNMATCHED": 0, "review": 0}

    for row in items:
        item_id, desc, equip_desc, qty_raw, qty_num = row
        if not desc:
            continue

        confidence = "UNMATCHED"
        method = "NONE"
        score = None
        oa_item_id = None
        review_flag = False
        suggested_name = ""
        suggested_cat = ""

        # Tier 1 — Keyword
        if oa_items:
            match = _keyword_match(desc, oa_items)
            if match:
                oa_item_id = match["id"]
                confidence = "HIGH"
                method = "KEYWORD"

        # Tier 2 — Fuzzy
        if confidence == "UNMATCHED" and oa_items:
            result = _fuzzy_match(desc, oa_items, high_thresh, review_thresh)
            if result:
                match, match_score = result
                oa_item_id = match["id"]
                score = match_score
                method = "FUZZY"
                if match_score >= high_thresh:
                    confidence = "PROBABLE"
                    review_flag = False
                else:
                    confidence = "PROBABLE"
                    review_flag = True  # score between review_thresh and high_thresh

        # Tier 3 — LLM
        if confidence == "UNMATCHED":
            if oa_items:
                llm_result = _llm_match(desc, oa_items, api_key)
            else:
                llm_result = None

            if llm_result:
                match, suggested_cat = llm_result
                oa_item_id = match["id"]
                confidence = "LLM"
                method = "LLM"
            else:
                review_flag = True
                # Ask LLM for suggested name + category for gap tracking
                if api_key:
                    try:
                        from google import genai
                        from google.genai import types
                        client = genai.Client(api_key=api_key)
                        gap_prompt = (
                            f"Suggest a standardized name and category for this "
                            f"industrial spare part: '{desc}'. "
                            f"Return JSON: {{\"name\": \"...\", \"category\": \"...\"}}"
                        )
                        resp = client.models.generate_content(
                            model="gemini-2.5-flash",
                            contents=gap_prompt,
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                temperature=0.3,
                            ),
                        )
                        if resp.text:
                            gap_data = json.loads(resp.text)
                            suggested_name = gap_data.get("name", "")
                            suggested_cat = gap_data.get("category", "")
                    except Exception:
                        pass
                record_gap(desc, suggested_name, suggested_cat)

        # 3.6  Plain description
        plain_desc = generate_plain_description(desc, equip_desc or "", api_key)

        # 3.7  Insert
        insert_matched_item(
            extracted_item_id=item_id,
            oa_item_id=oa_item_id,
            confidence=confidence,
            method=method,
            score=score,
            plain_description=plain_desc,
            is_mandatory=False,   # Updated via cross-reference if needed
            is_applicable=True,
            review_flag=review_flag,
        )

        counts[confidence] += 1
        if review_flag:
            counts["review"] += 1

    log.info(
        "Phase 3: Done — HIGH=%d  PROBABLE=%d  LLM=%d  UNMATCHED=%d  review_flagged=%d",
        counts["HIGH"], counts["PROBABLE"], counts["LLM"],
        counts["UNMATCHED"], counts["review"],
    )
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s"
    )

    parser = argparse.ArgumentParser(description="Phase 3: OA Master matching for a lead")
    parser.add_argument("--lead-id", type=int, required=True)
    args = parser.parse_args()

    settings = _load_settings()
    counts = match_items_for_lead(args.lead_id, settings)
    print(f"\nMatching complete: {counts}")


if __name__ == "__main__":
    main()
