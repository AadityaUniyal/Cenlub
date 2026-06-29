"""
src/extraction/classifier.py
=============================
Keyword-score classifier that maps a PDF's text to one of the predefined
document classes.  No ML model required — purely regex-based scoring.
"""

from __future__ import annotations

import re
from typing import Dict, List

# Each class has a list of regex patterns.
# Score = total count of matches across all patterns.
_CLASS_PATTERNS: Dict[str, List[str]] = {
    "Email": [
        r"lead\s*(?:no|number)", r"rfq", r"subject:",
        r"dear\s+team", r"best\s+regards",
        r"inquiry\s+information", r"purchasing\s+team", r"sent\s+from",
    ],
    "LOS Specification": [
        r"lube\s+oil\s+system", r"lube\s+oil\s+skid", r"los\s+spec",
        r"operating\s+conditions", r"filtration\s+level",
        r"reservoir\s+details", r"control\s+panel\s+req",
        r"steam\s+turbine\s+lube",
    ],
    "BOM": [
        r"bill\s+of\s+materials", r"\bbom\b", r"material\s+spec",
        r"unit\s+price", r"item\s+no", r"pricing\s+information",
    ],
    "Mandatory Spares": [
        r"mandatory\s+spare", r"rotor\s+assembly",
        r"mechanical\s+seal", r"\bbearings\b", r"part\s+number",
    ],
    "O&M Spares": [
        r"o\s*&\s*m\s+spare", r"operation.*maintenance",
        r"recommended\s+spare", r"consumables", r"filter\s+elements",
    ],
    "Technical Specification": [
        r"technical\s+spec", r"equipment\s+spec",
        r"pump\s+details", r"motor\s+details",
        r"heat\s+exchanger\s+details", r"ie3\s+efficiency", r"ip55",
    ],
    "Drawings": [
        r"drawings?", r"ga\s+drawing", r"p&id", r"schematics?",
    ],
}


def classify(text: str) -> str:
    """
    Return the document class with the highest keyword match score.
    Returns 'Unknown' when the top score is below the minimum threshold.
    """
    low = text.lower()
    scores = {
        cls: sum(len(re.findall(pat, low)) for pat in patterns)
        for cls, patterns in _CLASS_PATTERNS.items()
    }
    best, score = max(scores.items(), key=lambda x: x[1])
    return best if score >= 2 else "Unknown"
