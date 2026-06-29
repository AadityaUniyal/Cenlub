"""
src/core/validators.py
======================
Engineering sanity checks and validation helpers.
All threshold comparisons live here so they are tested in one place.
"""

from __future__ import annotations

import re
from typing import Dict, List

from src.core.models import RMCData


# Default ASME / industry-standard bounds
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "flow_min": 5.0,
    "flow_max": 2000.0,
    "press_min": 1.0,
    "press_max": 50.0,
    "temp_min": 10.0,
    "temp_max": 100.0,
    "filt_min": 1.0,
    "filt_max": 100.0,
}

# All RMC scalar fields (excludes list fields)
RMC_SCALAR_FIELDS = [
    "lead_no", "project_name", "customer", "application", "industry",
    "flow_rate", "pressure", "temperature", "oil_grade", "filtration_level",
    "heat_exchanger_moc", "reservoir_moc", "control_panel_requirement",
]


def sanity_check(rmc: RMCData,
                 thresholds: Dict[str, float] | None = None) -> List[str]:
    """
    Compare extracted numeric values against engineering bounds.
    Returns a list of warning strings (empty → all clear).
    """
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    warnings: List[str] = []

    checks = [
        ("flow_rate", t["flow_min"], t["flow_max"], "LPM"),
        ("pressure", t["press_min"], t["press_max"], "Bar"),
        ("temperature", t["temp_min"], t["temp_max"], "Deg C"),
        ("filtration_level", t["filt_min"], t["filt_max"], "Microns"),
    ]
    for field, lo, hi, unit in checks:
        val = getattr(rmc, field)
        if val == "XX":
            continue
        m = re.search(r"(\d+(?:\.\d+)?)", val)
        if m:
            n = float(m.group(1))
            if not lo <= n <= hi:
                warnings.append(
                    f"[SANITY] {field}: {n} {unit} is outside "
                    f"expected range [{lo} – {hi} {unit}]"
                )
    return warnings


def confidence_score(rmc: RMCData) -> float:
    """Return % of RMC scalar fields that are not 'XX'."""
    filled = sum(
        1 for f in RMC_SCALAR_FIELDS if getattr(rmc, f, "XX") != "XX"
    )
    return round(filled / len(RMC_SCALAR_FIELDS) * 100, 1)


def validate_rmc(rmc: RMCData,
                 thresholds: Dict[str, float] | None = None) -> dict:
    """
    Full validation report combining completeness + sanity checks.
    Returns a dict usable by the API response and the CSV writer.
    """
    warnings = sanity_check(rmc, thresholds)
    missing = [f for f in RMC_SCALAR_FIELDS if getattr(rmc, f, "XX") == "XX"]
    for f in missing:
        warnings.append(f"[MISSING] Field '{f}' could not be extracted.")

    # Spares overlap check
    mand_set = {s.description.lower().strip() for s in rmc.mandatory_spares}
    om_set = {s.description.lower().strip() for s in rmc.om_spares}
    for dup in mand_set & om_set:
        warnings.append(
            f"[OVERLAP] '{dup}' appears in both Mandatory and O&M spares."
        )

    score = confidence_score(rmc)
    # Deduct 5 points per sanity / overlap warning
    penalty = sum(1 for w in warnings if w.startswith("[SANITY]") or w.startswith("[OVERLAP]"))
    score = max(0.0, round(score - penalty * 5, 1))

    return {
        "confidence_score": score,
        "missing_fields": missing,
        "warnings": warnings,
        "is_valid": len(warnings) == 0,
    }
