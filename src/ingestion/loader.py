"""
src/ingestion/loader.py
=======================
PDF ingestion: text extraction (PyMuPDF), table extraction (pdfplumber),
and OCR fallback (PaddleOCR) for scanned pages.
"""

from __future__ import annotations

import os
import logging
from typing import Dict, List

import fitz          # PyMuPDF
import pdfplumber

log = logging.getLogger(__name__)

# PaddleOCR is optional — gracefully skipped if not installed
try:
    from paddleocr import PaddleOCR as _PaddleOCR
    _PADDLE_OK = True
except ImportError:
    _PADDLE_OK = False
    log.warning("PaddleOCR not installed — scanned pages will use PyMuPDF fallback.")


# ── Public API ────────────────────────────────────────────────────────────────

def load_pdf(path: str) -> Dict:
    """
    Ingest one PDF file.

    Returns a dict:
        pdf_name   : str
        pages      : List[{page_no, text, is_scanned}]
        full_text  : str   (all pages joined)
        tables     : List[{page_no, data: List[List[str]]}]
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"PDF not found: {path}")

    pages = _extract_pages(path)
    tables = _extract_tables(path)

    full_text = "\n\n".join(p["text"] for p in pages)
    return {
        "pdf_name": os.path.basename(path),
        "pages": pages,
        "full_text": full_text,
        "tables": tables,
    }


# ── Internals ─────────────────────────────────────────────────────────────────

def _extract_pages(path: str) -> List[Dict]:
    doc = fitz.open(path)
    pages = []
    for idx, page in enumerate(doc):
        text = page.get_text()
        is_scanned = (
            len(text.strip()) < 50
            and (len(page.get_images(full=True)) > 0 or len(page.get_drawings()) > 0)
        )
        if is_scanned:
            log.info(f"Scanned page detected: {os.path.basename(path)} p.{idx + 1} — running OCR.")
            text = _ocr_page(path, idx)
        pages.append({"page_no": idx + 1, "text": text, "is_scanned": is_scanned})
    doc.close()
    return pages


def _extract_tables(path: str) -> List[Dict]:
    tables = []
    try:
        with pdfplumber.open(path) as pdf:
            for p_idx, page in enumerate(pdf.pages):
                for raw_table in (page.extract_tables() or []):
                    cleaned = [
                        [str(cell).strip() if cell else "" for cell in row]
                        for row in raw_table
                        if any(cell for cell in row)
                    ]
                    if cleaned:
                        tables.append({"page_no": p_idx + 1, "data": cleaned})
    except Exception as exc:
        log.warning(f"pdfplumber error on {path}: {exc}")
    return tables


def _ocr_page(path: str, page_idx: int) -> str:
    """Run PaddleOCR on one page image; fall back to PyMuPDF text."""
    if _PADDLE_OK:
        try:
            doc = fitz.open(path)
            pix = doc[page_idx].get_pixmap(dpi=150)
            tmp = f"_ocr_tmp_{page_idx}.png"
            pix.save(tmp)
            doc.close()
            ocr = _PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
            result = ocr.ocr(tmp, cls=True)
            os.remove(tmp)
            if result and result[0]:
                return "\n".join(line[1][0] for line in result[0])
        except Exception as exc:
            log.warning(f"OCR failed (page {page_idx + 1}): {exc}")

    # Fallback
    doc = fitz.open(path)
    text = doc[page_idx].get_text()
    doc.close()
    return text
