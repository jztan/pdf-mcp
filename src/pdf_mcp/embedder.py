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
            "It ships with the default install; restore it with: "
            "pip install fastembed"
        ) from exc
    supported = {m["model"] for m in TextEmbedding.list_supported_models()}
    if model_name not in supported:
        names = ", ".join(sorted(supported))
        raise ValueError(
            f"Unknown embedding model '{model_name}'. "
            f"Supported fastembed models: {names}"
        )


def _cuda_requested() -> bool:
    """True when PDF_MCP_CUDA asks for the GPU. Unset means CPU, as before."""
    import os

    return os.environ.get("PDF_MCP_CUDA", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _add_nvidia_dll_dirs() -> None:
    """Put the nvidia-*-cu12 wheel DLLs where the Windows loader will find them.

    onnxruntime-gpu ships the CUDA provider but not the runtime it links
    against; the pip wheels put that runtime in site-packages/nvidia/*/bin,
    which is on nobody's PATH. Without this the provider fails to load and
    onnxruntime falls back to CPU, which is correct but ~400x slower.

    Prepending to PATH is what works: os.add_dll_directory alone is not enough,
    because the dependency chain between the NVIDIA DLLs is resolved by the
    loader's default search order. Only called when CUDA was asked for.
    """
    import glob
    import os
    import sysconfig

    if os.name != "nt":
        return
    base = os.path.join(sysconfig.get_paths()["purelib"], "nvidia")
    dirs = glob.glob(os.path.join(base, "*", "bin"))
    if not dirs:
        return
    os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ["PATH"]
    for directory in dirs:
        os.add_dll_directory(directory)


def _providers(model: Any) -> list[str]:
    """Execution providers the loaded session got, or [] if unreadable."""
    session = getattr(getattr(model, "model", model), "model", None)
    if session is None:
        return []
    try:
        return list(session.get_providers())
    except Exception:  # pragma: no cover - defensive
        return []


def _cuda_model(model_name: str, embedding_cls: Any) -> Any:
    """A CUDA-backed embedder, or None with a warning saying why not.

    The warning is the reason this is not a bare try/except. onnxruntime falls
    back to CPU by design when the provider cannot load, and the vectors it
    then produces are correct - so nothing downstream can tell, and the only
    symptom is a wall clock. A silent 400x is worse than a failure.
    """
    import warnings

    _add_nvidia_dll_dirs()
    try:
        candidate = embedding_cls(model_name, cuda=True, device_ids=[0])
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        warnings.warn(
            f"PDF_MCP_CUDA is set but the CUDA session failed ({exc!r}); using CPU.",
            RuntimeWarning,
            stacklevel=3,
        )
        return None
    if "CUDAExecutionProvider" in _providers(candidate):
        return candidate
    warnings.warn(
        "PDF_MCP_CUDA is set but onnxruntime gave CPU. Embedding will be far "
        "slower. Install the GPU extra (pip install 'pdf-mcp[gpu]') or unset "
        "PDF_MCP_CUDA to choose CPU deliberately.",
        RuntimeWarning,
        stacklevel=3,
    )
    return None


def _get_model(model_name: str) -> Any:
    """Load embedding model on first call; reload if model_name changed.

    Uses the GPU only when PDF_MCP_CUDA is set. Unset - the default - is the
    CPU path unchanged, so an existing install behaves exactly as before.
    """
    global _model, _model_name_loaded
    if _model is None or _model_name_loaded != model_name:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ImportError(
                "pdf_search semantic mode requires the 'fastembed' package. "
                "It ships with the default install; restore it with: "
                "pip install fastembed"
            ) from exc
        model = _cuda_model(model_name, TextEmbedding) if _cuda_requested() else None
        _model = model if model is not None else TextEmbedding(model_name)
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
