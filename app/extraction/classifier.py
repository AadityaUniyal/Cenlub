import re

DOCUMENT_CLASSES = {
    "Email": [
        r"lead\s*(no|number)", r"rfq", r"subject:", r"dear\s+team",
        r"best\s+regards", r"inquiry\s+information",
        r"purchasing\s+team", r"sent\s+from"
    ],
    "LOS Specification": [
        r"lube\s+oil\s+system", r"lube\s+oil\s+skid",
        r"los\s+specification", r"operating\s+conditions",
        r"filtration\s+level", r"reservoir\s+details",
        r"control\s+panel\s+requirements?", r"steam\s+turbine\s+lube"
    ],
    "BOM": [
        r"bill\s+of\s+materials", r"bom",
        r"material\s+spec(ification)?", r"unit\s+price",
        r"item\s+numbers?", r"pricing\s+information"
    ],
    "Mandatory Spares": [
        r"mandatory\s+spares?", r"mandatory\s+spare\s+lists?",
        r"rotor\s+assembly", r"mechanical\s+seal", r"bearings",
        r"part\s+number"
    ],
    "O&M Spares": [
        r"o\s*&\s*m\s+spares?", r"operation(al)?\s*&\s*maintenance",
        r"recommended\s+replacement", r"recommended\s+spares?",
        r"consumables", r"filter\s+elements"
    ],
    "Technical Specification": [
        r"technical\s+spec(ification)?",
        r"equipment\s+specifications?", r"pump\s+details",
        r"motor\s+details", r"heat\s+exchanger\s+details",
        r"instrumentation\s+requirements?", r"ie3\s+efficiency", r"ip55"
    ],
    "Drawings": [
        r"drawings?", r"ga\s+drawing", r"p&id", r"schematics?",
        r"piping\s+layout"
    ]
}


def classify_text(text: str) -> str:
    """
    Classifies the document text into one of the predefined classes.
    Computes a score based on keyword match frequencies.
    """
    text_lower = text.lower()
    scores = {cls: 0 for cls in DOCUMENT_CLASSES}

    for cls, patterns in DOCUMENT_CLASSES.items():
        for pattern in patterns:
            # Count the occurrences of this pattern in the text
            matches = len(re.findall(pattern, text_lower))
            scores[cls] += matches

    # Find class with max score
    max_score = 0
    best_class = "Unknown"

    for cls, score in scores.items():
        if score > max_score:
            max_score = score
            best_class = cls

    # If the max score is too low, we classify as Unknown
    if max_score < 2:
        return "Unknown"

    return best_class
