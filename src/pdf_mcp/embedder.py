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


_CUDA_ON = {"1", "true", "yes", "on"}
_CUDA_OFF = {"0", "false", "no", "off"}


def _cuda_setting() -> str | None:
    """What PDF_MCP_CUDA asks for: "on", "off", or None when unset/unknown.

    Unset leaves the choice to fastembed, whose default is auto-detect:
    CPU on a plain install, but the GPU on a machine where onnxruntime-gpu
    and a CUDA runtime are both reachable (a system-wide CUDA toolkit on
    the library path is enough; measured on an AWS Deep Learning AMI,
    benchmark_data/cuda_embedding/RESULTS.md). "off" is the only spelling
    that guarantees the CPU path on such a machine.
    """
    import os

    value = os.environ.get("PDF_MCP_CUDA", "").strip().lower()
    if value in _CUDA_ON:
        return "on"
    if value in _CUDA_OFF:
        return "off"
    return None


def _cuda_requested() -> bool:
    """True when PDF_MCP_CUDA asks for the GPU."""
    return _cuda_setting() == "on"


def _preload_cuda_runtime() -> None:
    """Make the CUDA runtime from the nvidia wheels loadable.

    onnxruntime-gpu ships the CUDA provider but not the runtime it links
    against. The pip wheels put that runtime under site-packages/nvidia/, which
    neither loader searches by default, so the provider fails to resolve its
    dependencies and onnxruntime falls back to the CPU - correct answers, far
    slower, and nothing in the output says why.

    Both platforms need help and they need different help. Windows takes the
    directories: PATH as well as add_dll_directory, because the NVIDIA DLLs
    depend on each other and that chain is resolved by the default search
    order. Linux cannot be helped by a directory at all - LD_LIBRARY_PATH is
    read by the dynamic loader before the interpreter starts - so the libraries
    are opened by hand with RTLD_GLOBAL, which puts them in the process where
    the provider will find them.

    Only called when CUDA was requested, and silent when the wheels are absent.
    """
    import glob
    import os
    import sys
    import sysconfig

    # Searched recursively because the layout is not stable across CUDA
    # series: the 12 wheels put DLLs in nvidia/<component>/bin, the 13 ones in
    # nvidia/cu13/bin/<arch>. Globbing one level found the CUDA 12 files and
    # silently missed the CUDA 13 ones, which reads as a machine without a GPU.
    base = os.path.join(sysconfig.get_paths()["purelib"], "nvidia")
    if sys.platform == "win32":
        found = glob.glob(os.path.join(base, "**", "*.dll"), recursive=True)
        dirs = sorted({os.path.dirname(dll) for dll in found})
        if not dirs:
            return
        joined = os.pathsep.join(dirs)
        os.environ["PATH"] = joined + os.pathsep + os.environ["PATH"]
        for directory in dirs:
            os.add_dll_directory(directory)
        return

    import ctypes

    libs = glob.glob(os.path.join(base, "**", "*.so.*"), recursive=True)

    # libcublasLt before libcublas, and both before everything else. Taken from
    # PyTorch's _preload_cuda_deps, which documents why: libcublas resolves
    # libcublasLt through its own RUNPATH, so on a host that also has a
    # system-wide CUDA it can bind to a different version and fail later with
    # missing symbols. Loading the wheel's copy first settles it. Alphabetical
    # order puts them the wrong way round.
    def first(path: str) -> tuple[int, str]:
        name = os.path.basename(path)
        if name.startswith("libcublasLt."):
            return (0, name)
        if name.startswith("libcublas."):
            return (1, name)
        return (2, name)

    for lib in sorted(libs, key=first):
        try:
            ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
        except OSError:
            # One unloadable library is not fatal: the provider needs a subset,
            # and which subset depends on the build. If it turns out to need
            # this one, the caller's provider check reports it.
            continue


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

    _preload_cuda_runtime()
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
    # The likely causes, in the order they happen, because this is the only
    # place the environment's shape is visible. A resolver cannot check any of
    # them: two distributions share this import path, and the CUDA series a
    # machine needs depends on the card.
    warnings.warn(
        "PDF_MCP_CUDA is set but onnxruntime gave CPU, so embedding will be far "
        "slower. Usually onnxruntime is installed alongside onnxruntime-gpu and "
        "wins the shared import path, or the CUDA runtime is absent or is the "
        "wrong series for the GPU. Unset PDF_MCP_CUDA to choose CPU "
        "deliberately.",
        RuntimeWarning,
        stacklevel=3,
    )
    return None


def _get_model(model_name: str) -> Any:
    """Load embedding model on first call; reload if model_name changed.

    PDF_MCP_CUDA=1 asks for the GPU and warns if it did not get it;
    PDF_MCP_CUDA=0 pins the CPU; unset lets fastembed auto-detect, which is
    the CPU on a plain install and the same constructor call as before.
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
        setting = _cuda_setting()
        model = _cuda_model(model_name, TextEmbedding) if setting == "on" else None
        if model is None:
            # cuda=False pins CPUExecutionProvider; the bare constructor
            # leaves fastembed's auto-detect in charge.
            model = (
                TextEmbedding(model_name, cuda=False)
                if setting == "off"
                else TextEmbedding(model_name)
            )
        _model = model
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
