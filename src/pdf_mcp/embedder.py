"""
Thin wrapper around fastembed for lazy model loading and text embedding.

The embedding model is loaded once per process (singleton). fastembed is an
optional dependency; calling encode() when it is not installed raises
ImportError with an actionable install hint.
"""

from __future__ import annotations

from typing import Any

MODEL_NAME = "BAAI/bge-small-en-v1.5"

# Module-level singleton. None until the first encode() call.
_model: Any = None


def check_available() -> None:
    """
    Raise ImportError with install hint if fastembed is not installed.

    Call this at the start of pdf_semantic_search to give a clear error
    before any expensive PDF work begins.
    """
    try:
        import fastembed  # type: ignore[import-untyped]  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "pdf_semantic_search requires the 'fastembed' package. "
            "Install it with: pip install 'pdf-mcp[semantic]'"
        ) from exc


def _get_model() -> Any:
    """Load embedding model on first call; return cached model on later calls."""
    global _model
    if _model is None:
        try:
            from fastembed import TextEmbedding  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "pdf_semantic_search requires the 'fastembed' package. "
                "Install it with: pip install 'pdf-mcp[semantic]'"
            ) from exc
        _model = TextEmbedding(MODEL_NAME)
    return _model


def encode(texts: list[str]) -> Any:
    """
    Encode a list of texts into embedding vectors.

    Returns an ndarray of shape (N, 384), dtype float32.
    Vectors are L2-normalized by fastembed (dot product == cosine similarity).
    """
    import numpy as np  # type: ignore[import-untyped]

    model = _get_model()
    embeddings = list(model.embed(texts))
    return np.array(embeddings, dtype=np.float32)


def encode_query(text: str) -> Any:
    """
    Encode a single query string.

    Returns an ndarray of shape (384,), dtype float32.
    """
    return encode([text])[0]
