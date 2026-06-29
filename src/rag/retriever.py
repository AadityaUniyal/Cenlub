"""
src/rag/retriever.py
=====================
FAISS-backed parent-child RAG index for semantic document retrieval.
Requires: faiss-cpu, sentence-transformers
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

try:
    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer

    _MODEL: Optional[SentenceTransformer] = None

    def _embed(texts: List[str]):
        global _MODEL
        if _MODEL is None:
            log.info("Loading SentenceTransformer (all-MiniLM-L6-v2)…")
            _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        return np.array(_MODEL.encode(texts, show_progress_bar=False), dtype=np.float32)

    _FAISS_OK = True
except ImportError:
    _FAISS_OK = False
    log.warning("faiss-cpu or sentence-transformers not installed — RAG disabled.")


def _split(text: str, chunk: int, overlap: int) -> List[str]:
    segs, start = [], 0
    while start < len(text):
        end = start + chunk
        seg = text[start:end]
        if end < len(text):
            sp = seg.rfind(" ")
            if sp > chunk // 2:
                seg = text[start: start + sp]
                end = start + sp
        if seg.strip():
            segs.append(seg.strip())
        start += chunk - overlap
    return segs


class RAGIndex:
    """Parent-child FAISS index over ingested documents."""

    DIM = 384   # all-MiniLM-L6-v2

    def __init__(self):
        if not _FAISS_OK:
            raise RuntimeError("FAISS / sentence-transformers not available.")
        self._index = faiss.IndexFlatL2(self.DIM)
        self._parents: List[Dict] = []
        self._children: List[Dict] = []

    def add_documents(self, documents: list, tables_by_doc: dict = None) -> None:
        texts, new_children = [], []
        for doc in documents:
            pdf = doc["pdf_name"]
            tbl_map: Dict[int, List] = {}
            for t in (tables_by_doc or {}).get(pdf, []):
                tbl_map.setdefault(t["page_no"], []).append(t)

            for page in doc["pages"]:
                p_no, p_text = page["page_no"], page["text"]
                for p_seg in _split(p_text, 1200, 200):
                    p_idx = len(self._parents)
                    self._parents.append({"text": p_seg, "source_file": pdf,
                                          "page_no": p_no, "type": "text"})
                    for c_seg in _split(p_seg, 200, 50):
                        new_children.append({"text": c_seg, "parent_idx": p_idx})
                        texts.append(c_seg)

                for tbl in tbl_map.get(p_no, []):
                    rows = tbl["data"]
                    if not rows:
                        continue
                    md = "\n".join("| " + " | ".join(r) + " |" for r in rows)
                    p_idx = len(self._parents)
                    self._parents.append({"text": md, "source_file": pdf,
                                          "page_no": p_no, "type": "table"})
                    for c_seg in _split(md, 200, 50):
                        new_children.append({"text": c_seg, "parent_idx": p_idx})
                        texts.append(c_seg)

        if texts:
            self._index.add(_embed(texts))
            self._children.extend(new_children)
            log.info(f"Indexed {len(texts)} child chunks → {len(self._parents)} parents.")

    def query(self, query: str, top_k: int = 5) -> List[Dict]:
        if not self._children:
            return []
        q_emb = _embed([query])
        dists, idxs = self._index.search(q_emb, min(top_k * 2, len(self._children)))
        results, seen = [], set()
        for dist, idx in zip(dists[0], idxs[0]):
            if idx < 0 or idx >= len(self._children):
                continue
            p_idx = self._children[idx]["parent_idx"]
            if p_idx not in seen:
                seen.add(p_idx)
                entry = dict(self._parents[p_idx])
                entry["score"] = float(dist)
                results.append(entry)
                if len(results) >= top_k:
                    break
        return results
