"""
src/llm/gemini_client.py
=========================
Gemini-based LLM agents for RMC extraction and auditing.

Two agents:
  extractor_agent  — first pass: fills in fields from the tender text
  auditor_agent    — second pass: verifies and corrects the extraction

Both return a plain Python dict matching the RMCData field names.
Falls back to the rule-based dict if Gemini is unavailable.
"""

from __future__ import annotations

import json
import logging
from typing import Dict

log = logging.getLogger(__name__)


# ── Client factory ───────────────────────────────────────────────────────

def _client(api_key: str):
    from google import genai
    return genai.Client(api_key=api_key)


# ── Shared prompt fragments ──────────────────────────────────────────────

_RMC_JSON_KEYS = (
    "lead_no, project_name, customer, application, industry, "
    "flow_rate, pressure, temperature, oil_grade, filtration_level, "
    "heat_exchanger_moc, reservoir_moc, control_panel_requirement"
)

_UNIT_RULES = (
    "Normalise units: flow → LPM, pressure → Bar, "
    "temperature → Deg C, filtration → Microns. "
    "For MOC use: SS304, SS316, Carbon Steel, or Copper."
)

_SPARES_KEYS = (
    "mandatory_spares: [{item_no, description, qty, part_no}]\n"
    "om_spares:        [{item_no, description, qty, frequency}]"
)


# ── Extractor agent ──────────────────────────────────────────────────────

def extractor_agent(
    full_text: str,
    rule_based: Dict,
    api_key: str,
    few_shot_example: str = "",
) -> Dict:
    """
    First-pass Gemini agent.  Uses rule-based dict as a starting point and
    fills any 'XX' fields from the tender text.
    Returns a plain dict on success, or *rule_based* on failure.
    """
    if not api_key:
        log.info("No Gemini key — using rule-based extraction only.")
        return rule_based

    few_shot_section = ""
    if few_shot_example:
        few_shot_section = (
            f"\nHere is a relevant past RFQ and its approved "
            f"extraction:\n---\n{few_shot_example}\n---\n"
        )

    prompt = (
        f"""You are an expert industrial tender extraction agent.
{few_shot_section}
Rule-based extraction (fields marked 'XX' were not found):
{json.dumps(rule_based, indent=2, default=str)}

Tender document text (first 30 000 chars):
---
{full_text[:30000]}
---

Return a JSON object with EXACTLY these keys """
        f"""(use "XX" if a value is genuinely absent):
{_RMC_JSON_KEYS}

Also return:
{_SPARES_KEYS}

Rules:
- Prefer the most specific and most recently stated value.
- {_UNIT_RULES}
- Do NOT invent values not present in the text.
"""
    )
    try:
        from google.genai import types
        resp = _client(api_key).models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        if resp.text:
            log.info("Gemini extractor: success.")
            return json.loads(resp.text)
    except Exception as exc:
        log.warning(
            f"Gemini extractor failed: {exc} — using rule-based fallback."
        )

    return rule_based


# ── Auditor agent ────────────────────────────────────────────────────────

def auditor_agent(full_text: str, initial: Dict, api_key: str) -> Dict:
    """
    Second-pass critic/auditor agent.
    Verifies the initial extraction against the source text, corrects
    hallucinations, and fills remaining 'XX' fields.
    Returns the corrected dict, or *initial* on failure.
    """
    if not api_key:
        return initial

    prompt = (
        f"""You are a critic-auditor agent reviewing a prior extraction.

Initial extraction JSON:
{json.dumps(initial, indent=2, default=str)}

Original tender text (first 30 000 chars):
---
{full_text[:30000]}
---

AUDIT RULES:
1. Verify every value exists verbatim (or close paraphrase) in """
        f"""the source text.
2. If a value is wrong or hallucinated, correct it from the actual text.
3. Fill any remaining 'XX' fields if the information is present.
4. Priority order: RFQ Email > LOS Specification > Technical Spec > BOM.
5. {_UNIT_RULES}
6. Keep the EXACT same JSON structure — same keys, same spares """
        f"""sub-arrays.

Return the final audited JSON.
"""
    )
    try:
        from google.genai import types
        resp = _client(api_key).models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        if resp.text:
            log.info("Gemini auditor: success.")
            return json.loads(resp.text)
    except Exception as exc:
        log.warning(
            f"Gemini auditor failed: {exc} — keeping initial extraction."
        )

    return initial


# ── RAG document chat ────────────────────────────────────────────────────

def answer_query(query: str, context_chunks: list, api_key: str) -> str:
    """
    Answer an engineering query using retrieved document chunks as context.
    """
    if not api_key:
        return "Error: GEMINI_API_KEY not set."

    formatted = []
    for chunk in context_chunks:
        kind = "Table" if chunk.get("type") == "table" else "Text"
        formatted.append(
            f"{kind} [{chunk['source_file']} "
            f"p.{chunk['page_no']}]:\n{chunk['text']}"
        )
    context_str = "\n\n---\n\n".join(formatted)

    prompt = (
        "You are an expert engineering assistant. "
        "Answer the following question using ONLY the provided document "
        "contexts. Cite the file name and page number in your answer.\n\n"
        f"Question: {query}\n\n"
        f"Contexts:\n{context_str}\n\nAnswer:"
    )
    try:
        resp = _client(api_key).models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        return resp.text or "No response generated."
    except Exception as exc:
        log.error(f"Gemini query failed: {exc}")
        return f"Error: {exc}"


# ── Phase 2A.3: Plain description generation ─────────────────────────────

def generate_plain_descriptions(rows: list, api_key: str) -> list:
    """
    Phase 2A.3: For each applicable BOM row without a plain_description,
    call Gemini to produce a 1–2 sentence plain-language summary.

    Batches items to minimise API calls (up to 10 per request).
    Returns the rows list with plain_description fields populated.
    Falls back gracefully if Gemini is unavailable.
    """
    if not api_key:
        log.info("No Gemini key — skipping plain description generation.")
        return rows

    items_needing = [
        (i, r) for i, r in enumerate(rows)
        if (r.get("applicable_flag", True) and
            not r.get("plain_description") and
            r.get("item_description"))
    ]

    if not items_needing:
        return rows

    BATCH = 10
    for batch_start in range(0, len(items_needing), BATCH):
        batch = items_needing[batch_start: batch_start + BATCH]
        numbered = "\n".join(
            f"{j + 1}. {r['item_description']}"
            for j, (_, r) in enumerate(batch)
        )
        prompt = (
            "You are a technical writer for an industrial equipment "
            "company.\n"
            "For each spare part below, write exactly ONE sentence "
            "(max 30 words) explaining what it is and why it is stocked "
            "as a maintenance spare.\n"
            "Return a JSON array of strings, one per item, in the same "
            "order.\n\n"
            f"Items:\n{numbered}"
        )
        try:
            from google.genai import types
            resp = _client(api_key).models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.3,
                ),
            )
            if resp.text:
                import json as _json
                descriptions = _json.loads(resp.text)
                for j, (orig_idx, _) in enumerate(batch):
                    if j < len(descriptions):
                        rows[orig_idx]["plain_description"] = str(
                            descriptions[j]
                        ).strip()
                log.info(
                    f"Plain descriptions generated for {len(batch)} items."
                )
        except Exception as exc:
            log.warning(
                f"Plain description generation failed "
                f"(batch {batch_start}): {exc}"
            )

    return rows


# ── Phase 5: Checklist auto-fill ────────────────────────────────────────

def fill_checklist_from_text(full_text: str, api_key: str) -> dict:
    """
    Phase 5: Use Gemini to extract and populate the 20 LOS parameter fields
    and 15 document checklist items from the combined tender text.
    Returns a dict matching ChecklistDoc fields.
    Falls back to empty dict on failure.
    """
    if not api_key:
        return {}

    prompt = (
        f"""You are an expert at reading industrial tender documents for """
        f"""lube oil systems.

Extract the following fields from the text below. Return a JSON object.

LOS Parameters (use "XX" if not found):
los_application, los_industry, los_epc, los_consultant, los_project,
los_pressure_outlet, los_pressure_pump, los_flow_rate, los_oil_grade,
los_temp_oil_in, los_temp_oil_out, los_temp_water_in, """
        f"""los_temp_water_out,
los_moc_heat_exchanger, los_filtration_micron, los_area_zone,
los_transmitter (Y/N), los_control_panel (Y/N), los_junction_box (Y/N),
los_lcs (Y/N), los_tank (Y/N), los_moc_reservoir

Document availability (RECEIVED / MISSING for each):
pid_drawing, ga_drawing, customer_ts_los, vendor_list, mech_spec,
elec_spec, instrumentation_spec, pms, other_docs, control_narrative,
welding_spec, painting_spec, packing_spec, water_properties, """
        f"""geological_conditions

Tender text:
---
{full_text[:25000]}
---
"""
    )
    try:
        from google.genai import types
        resp = _client(api_key).models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        if resp.text:
            import json as _json
            return _json.loads(resp.text)
    except Exception as exc:
        log.warning(f"Checklist fill failed: {exc}")
    return {}
