"""
Thin wrapper around fastembed for lazy model loading and text embedding.

The embedding model is loaded once per process (singleton). If the configured
model name changes mid-process, the singleton reloads automatically.
fastembed is an optional dependency; calling encode() when it is not installed
raises ImportError with an actionable install hint.

Note: _get_model is not thread-safe. This is intentional — FastMCP uses
asyncio with a single thread for STDIO transport, so concurrent access cannot
occur in normal operation.
"""

from __future__ import annotations

from typing import Any

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

# Module-level singleton. None until the first encode() call.
_model: Any = None
_model_name_loaded: str | None = None


def check_available(model_name: str) -> None:
    """
    Raise ImportError (fastembed missing) or ValueError (unknown model name).

    Call this before running semantic search to surface config errors
    before any expensive PDF work begins.
    """
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise ImportError(
            "pdf_search semantic mode requires the 'fastembed' package. "
            "Install it with: pip install 'pdf-mcp[semantic]'"
        ) from exc
    supported = {m["model"] for m in TextEmbedding.list_supported_models()}
    if model_name not in supported:
        names = ", ".join(sorted(supported))
        raise ValueError(
            f"Unknown embedding model '{model_name}'. "
            f"Supported fastembed models: {names}"
        )


def _get_model(model_name: str) -> Any:
    """Load embedding model on first call; reload if model_name changed."""
    global _model, _model_name_loaded
    if _model is None or _model_name_loaded != model_name:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ImportError(
                "pdf_search semantic mode requires the 'fastembed' package. "
                "Install it with: pip install 'pdf-mcp[semantic]'"
            ) from exc
        _model = TextEmbedding(model_name)
        _model_name_loaded = model_name
    return _model


def encode(texts: list[str], model_name: str) -> Any:
    """
    Encode a list of texts into embedding vectors.

    Returns an ndarray of shape (N, D), dtype float32, L2-normalized so that
    a dot product equals cosine similarity. We normalize here rather than rely
    on the model: fastembed 0.8 returns unnormalized vectors for some models
    (e.g. multilingual-e5-large, norm ~28 after its CLS->mean pooling change),
    which would otherwise break semantic scoring in server.py.
    """
    import numpy as np  # type: ignore[import-untyped]

    model = _get_model(model_name)
    arr = np.array(list(model.embed(texts)), dtype=np.float32)
    if arr.size == 0:
        return arr
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.clip(norms, 1e-12, None)


def encode_query(text: str, model_name: str) -> Any:
    """
    Encode a single query string.

    Returns an ndarray of shape (D,), dtype float32.
    """
    return encode([text], model_name)[0]
