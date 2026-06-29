from sentence_transformers import SentenceTransformer
import numpy as np
import logging

logger = logging.getLogger(__name__)

_model = None


def get_embedding_model():
    """Loads and caches the SentenceTransformer model."""
    global _model
    if _model is None:
        logger.info("Loading SentenceTransformer model 'all-MiniLM-L6-v2'...")
        # Since it is a small model (~90MB), it downloads and loads very quickly
        _model = SentenceTransformer('all-MiniLM-L6-v2')
    return _model


def get_embeddings(texts: list) -> np.ndarray:
    """Encodes a list of texts into vector embeddings."""
    if not texts:
        return np.empty((0, 384), dtype=np.float32)

    model = get_embedding_model()
    embeddings = model.encode(texts, show_progress_bar=False)
    return np.array(embeddings, dtype=np.float32)
