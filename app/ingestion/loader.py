import fitz  # PyMuPDF
import pdfplumber
import os
import logging

logger = logging.getLogger(__name__)


def extract_text_from_pdf(pdf_path: str) -> dict:
    """
    Extracts text page-by-page from a PDF using PyMuPDF.
    Detects scanned pages if text content is extremely low.
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF file not found at {pdf_path}")

    doc = fitz.open(pdf_path)
    pages = []
    full_text_list = []

    for page_idx, page in enumerate(doc):
        text = page.get_text()
        # Clean up text
        cleaned_text = text.strip()

        # Determine if the page is scanned
        # If the page contains images/drawings but less than 50 characters of
        # text, classify as scanned.
        is_scanned = False
        if len(cleaned_text) < 50:
            # Check if there are images or vector graphics on the page
            image_list = page.get_images(full=True)
            drawings = page.get_drawings()
            if len(image_list) > 0 or len(drawings) > 0:
                is_scanned = True

        pages.append({
            "page_no": page_idx + 1,
            "text": text,
            "is_scanned": is_scanned
        })
        full_text_list.append(text)

    return {
        "pdf_name": os.path.basename(pdf_path),
        "pages": pages,
        "full_text": "\n\n".join(full_text_list)
    }


def extract_tables_from_pdf(pdf_path: str) -> list:
    """
    Extracts tabular data page-by-page from a PDF using pdfplumber.
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF file not found at {pdf_path}")

    tables = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            extracted_tables = page.extract_tables()
            for t_idx, table in enumerate(extracted_tables):
                # Clean table data: remove empty rows and strip whitespace
                cleaned_table = []
                for row in table:
                    # Keep rows that aren't entirely None or empty
                    if any(cell is not None for cell in row):
                        cleaned_row = [
                            str(cell).strip() if cell is not None else "" for cell in row]
                        cleaned_table.append(cleaned_row)

                if cleaned_table:
                    tables.append({
                        "page_no": page_idx + 1,
                        "table_index": t_idx,
                        "data": cleaned_table
                    })
    return tables
