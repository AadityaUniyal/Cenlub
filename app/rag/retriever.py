import faiss
import logging
from app.rag.embeddings import get_embeddings

logger = logging.getLogger(__name__)


def split_into_segments(text: str, chunk_size: int, overlap: int) -> list:
    """Splits a string into overlapping segments of chunk_size."""
    segments = []
    text_len = len(text)
    if text_len <= chunk_size:
        if text.strip():
            segments.append(text.strip())
        return segments

    start = 0
    while start < text_len:
        end = start + chunk_size
        segment = text[start:end]

        # Adjust end to word boundary
        if end < text_len:
            last_space = segment.rfind(" ")
            if last_space > chunk_size // 2:
                end = start + last_space
                segment = text[start:end]

        cleaned = segment.strip()
        if cleaned:
            segments.append(cleaned)

        start += (chunk_size - overlap)
    return segments


class RAGIndex:
    def __init__(self):
        self.dimension = 384  # all-MiniLM-L6-v2 dimension
        self.index = faiss.IndexFlatL2(self.dimension)

        # Parent-Child index structure
        self.parent_chunks = []  # List of {"text": str, "source_file": str, "page_no": int, "type": "text"|"table"}
        self.child_chunks = []   # List of {"text": str, "parent_idx": int}

    def add_documents(self, documents: list, tables_by_doc: dict = None):
        """
        Adds documents and their tables to the index.
        documents is a list of dicts: {"pdf_name": str, "pages": [{"page_no": int, "text": str, ...}]}
        tables_by_doc is a dict mapping pdf_name -> list of tables (each table is {"page_no": int, "data": [[cell, ...], ...]})
        """
        all_child_texts = []
        new_child_chunks = []

        for doc in documents:
            pdf_name = doc["pdf_name"]
            doc_tables = (tables_by_doc or {}).get(pdf_name, [])

            # Group tables by page for easy alignment
            tables_by_page = {}
            for table in doc_tables:
                p_no = table["page_no"]
                if p_no not in tables_by_page:
                    tables_by_page[p_no] = []
                tables_by_page[p_no].append(table)

            for page in doc["pages"]:
                page_no = page["page_no"]
                page_text = page["text"]

                # 1. Process Page Text (Parent & Child Chunks)
                parent_segments = split_into_segments(page_text, chunk_size=1200, overlap=200)

                for p_seg in parent_segments:
                    parent_idx = len(self.parent_chunks)
                    self.parent_chunks.append({
                        "text": p_seg,
                        "source_file": pdf_name,
                        "page_no": page_no,
                        "type": "text"
                    })

                    # Split parent into child segments
                    child_segments = split_into_segments(p_seg, chunk_size=200, overlap=50)
                    for c_seg in child_segments:
                        new_child_chunks.append({
                            "text": c_seg,
                            "parent_idx": parent_idx
                        })
                        all_child_texts.append(c_seg)

                # 2. Process Page Tables (Enrichment as separate chunks)
                page_tables = tables_by_page.get(page_no, [])
                for t_idx, table in enumerate(page_tables):
                    table_rows = table["data"]
                    if not table_rows:
                        continue

                    # Format table as Markdown
                    table_lines = [f"Table {t_idx + 1} on Page {page_no} of {pdf_name}:"]
                    for row in table_rows:
                        table_lines.append("| " + " | ".join(row) + " |")
                    table_markdown = "\n".join(table_lines)

                    parent_idx = len(self.parent_chunks)
                    self.parent_chunks.append({
                        "text": table_markdown,
                        "source_file": pdf_name,
                        "page_no": page_no,
                        "type": "table"
                    })

                    # Split table markdown into child segments so cells are indexed
                    child_segments = split_into_segments(table_markdown, chunk_size=200, overlap=50)
                    for c_seg in child_segments:
                        new_child_chunks.append({
                            "text": c_seg,
                            "parent_idx": parent_idx
                        })
                        all_child_texts.append(c_seg)

        if not all_child_texts:
            logger.warning("No text found to index.")
            return

        # Compute embeddings for children
        embeddings = get_embeddings(all_child_texts)

        # Add to FAISS
        self.index.add(embeddings)
        self.child_chunks.extend(new_child_chunks)
        logger.info(
            f"Indexed {len(all_child_texts)} child chunks (linked to {len(self.parent_chunks)} parent contexts) in FAISS.")

    def query(self, query_text: str, top_k: int = 5) -> list:
        """
        Queries the child index and returns corresponding parent contexts.
        """
        if not self.child_chunks:
            return []

        query_emb = get_embeddings([query_text])

        # Search FAISS index
        distances, indices = self.index.search(query_emb, min(top_k * 2, len(self.child_chunks)))

        results = []
        seen_parents = set()

        for dist, idx in zip(distances[0], indices[0]):
            if idx != -1 and idx < len(self.child_chunks):
                child = self.child_chunks[idx]
                parent_idx = child["parent_idx"]

                # Deduplicate parent contexts in results
                if parent_idx not in seen_parents:
                    seen_parents.add(parent_idx)
                    parent_data = self.parent_chunks[parent_idx].copy()
                    parent_data["score"] = float(dist)
                    results.append(parent_data)

                    if len(results) >= top_k:
                        break

        return results


class HistoricalIndex:
    def __init__(self):
        self.dimension = 384
        self.index = faiss.IndexFlatL2(self.dimension)
        self.records = []  # List of {"text": str, "rmc_json": str}

    def add_historical_record(self, raw_text: str, rmc_json: str):
        """Adds a historical RFQ and its validated RMC JSON mapping to the database."""
        if not raw_text.strip():
            return
        emb = get_embeddings([raw_text])
        self.index.add(emb)
        self.records.append({
            "text": raw_text,
            "rmc_json": rmc_json
        })
        logger.info("Indexed 1 historical RFQ quotation record in FAISS.")

    def get_similar_example(self, query_text: str) -> str:
        """Retrieves the most semantically similar past RMC as a few-shot learning example."""
        if not self.records:
            return ""

        query_emb = get_embeddings([query_text])
        distances, indices = self.index.search(query_emb, 1)
        idx = indices[0][0]

        if idx != -1 and idx < len(self.records):
            rec = self.records[idx]
            return f"SAMPLE PAST RFQ TEXT:\n{rec['text']}\n\nAPPROVED RMC OUTPUT:\n{rec['rmc_json']}"

        return ""
