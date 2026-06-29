import fitz
import os
import logging

logger = logging.getLogger(__name__)

# Flag to track PaddleOCR import status
PADDLE_OCR_AVAILABLE = False
try:
    from paddleocr import PaddleOCR
    PADDLE_OCR_AVAILABLE = True
except ImportError:
    logger.warning(
        "PaddleOCR is not installed. Scanned pages will be processed using standard text extraction.")


def perform_ocr_on_page(pdf_path: str, page_no: int) -> str:
    """
    Renders the specified PDF page as an image and runs PaddleOCR on it.
    If PaddleOCR is not available, falls back to standard PyMuPDF text extraction.
    """
    doc = fitz.open(pdf_path)
    if page_no < 1 or page_no > len(doc):
        raise IndexError(
            f"Page number {page_no} is out of bounds for {pdf_path}")

    page = doc[page_no - 1]

    if not PADDLE_OCR_AVAILABLE:
        logger.warning(
            f"PaddleOCR not available. Using standard text extraction for page {page_no}.")
        return page.get_text()

    # Render page to a temporary PNG image
    pix = page.get_pixmap(dpi=150)
    temp_img_path = f"temp_page_{page_no}.png"
    pix.save(temp_img_path)

    try:
        # Initialize PaddleOCR (English language, use GPU if available, though
        # CPU is default)
        ocr = PaddleOCR(use_angle_cls=True, lang='en', show_log=False)
        result = ocr.ocr(temp_img_path, cls=True)

        # Parse the OCR results
        extracted_lines = []
        if result and result[0]:
            for line in result[0]:
                text = line[1][0]  # Extract text content from result format
                extracted_lines.append(text)

        return "\n".join(extracted_lines)
    except Exception as e:
        logger.error(
            f"Error during OCR of page {page_no}: {
                str(e)}. Falling back to PyMuPDF.")
        return page.get_text()
    finally:
        # Clean up temporary image file
        if os.path.exists(temp_img_path):
            os.remove(temp_img_path)
