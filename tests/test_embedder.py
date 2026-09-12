"""Unit tests for pdf_mcp.embedder. All tests mock fastembed — no model download."""

import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

DEFAULT = "BAAI/bge-small-en-v1.5"


def test_check_available_raises_when_fastembed_missing():
    """check_available() raises ImportError with install hint when fastembed absent."""
    import pdf_mcp.embedder as emb

    with patch.dict(sys.modules, {"fastembed": None}):
        with pytest.raises(ImportError, match="pip install fastembed"):
            emb.check_available(DEFAULT)


def _make_mock_model(dim: int = 384) -> MagicMock:
    """Mock fastembed TextEmbedding that yields dim-dimensional unit vectors."""
    mock = MagicMock()
    mock.embed.side_effect = lambda texts, **_: (
        np.ones(dim, dtype=np.float32) for _ in texts
    )
    return mock


def test_encode_returns_shape_n_by_384():
    """encode(texts, model_name) returns ndarray of shape (N, 384), dtype float32."""
    import pdf_mcp.embedder as emb

    emb._model = _make_mock_model(384)
    emb._model_name_loaded = DEFAULT
    try:
        result = emb.encode(["hello", "world", "foo"], DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    assert result.shape == (3, 384)
    assert result.dtype == np.float32


def test_encode_query_returns_1d_vector_of_384():
    """encode_query(text, model_name) returns ndarray of shape (384,), dtype float32."""
    import pdf_mcp.embedder as emb

    emb._model = _make_mock_model(384)
    emb._model_name_loaded = DEFAULT
    try:
        result = emb.encode_query("what is revenue?", DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    assert result.shape == (384,)
    assert result.dtype == np.float32


def test_encode_raises_when_fastembed_missing():
    """encode() raises ImportError with install hint when fastembed absent."""
    import pdf_mcp.embedder as emb

    emb._model = None
    emb._model_name_loaded = None
    try:
        with patch.dict(sys.modules, {"fastembed": None}):
            with pytest.raises(ImportError, match="pip install fastembed"):
                emb.encode(["hello"], DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None


def _make_unnormalized_mock(vec: list) -> MagicMock:
    """Mock TextEmbedding that yields a fixed UNnormalized vector per text."""
    mock = MagicMock()
    mock.embed.side_effect = lambda texts, **_: (
        np.array(vec, dtype=np.float32) for _ in texts
    )
    return mock


def test_encode_l2_normalizes_unnormalized_vectors():
    """encode() returns unit-norm rows even when the model yields raw vectors.

    Regression for fastembed 0.8 returning unnormalized e5 vectors (norm ~28),
    which broke the dot==cosine contract in semantic scoring.
    """
    import pdf_mcp.embedder as emb

    # norm-5 vector -> normalized should be [0.6, 0.8, 0, 0]
    emb._model = _make_unnormalized_mock([3.0, 4.0, 0.0, 0.0])
    emb._model_name_loaded = DEFAULT
    try:
        result = emb.encode(["a", "b"], DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    norms = np.linalg.norm(result, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-6)
    assert np.allclose(result[0], [0.6, 0.8, 0.0, 0.0], atol=1e-6)


def test_encode_query_returns_unit_vector():
    """encode_query() returns a unit-norm vector regardless of model output norm."""
    import pdf_mcp.embedder as emb

    emb._model = _make_unnormalized_mock([0.0, 3.0, 4.0])
    emb._model_name_loaded = DEFAULT
    try:
        result = emb.encode_query("q", DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    assert result.shape == (3,)
    assert np.isclose(np.linalg.norm(result), 1.0, atol=1e-6)


def test_encode_empty_list_returns_empty_without_error():
    """encode([]) returns an empty array, not a normalization crash."""
    import pdf_mcp.embedder as emb

    emb._model = _make_mock_model(384)
    emb._model_name_loaded = DEFAULT
    try:
        result = emb.encode([], DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    assert result.shape[0] == 0


def test_singleton_model_constructed_once():
    """TextEmbedding constructor is called only once across multiple encode() calls."""
    import pdf_mcp.embedder as emb

    emb._model = None
    emb._model_name_loaded = None

    mock_instance = _make_mock_model(384)
    mock_cls = MagicMock(return_value=mock_instance)
    mock_fastembed = MagicMock()
    mock_fastembed.TextEmbedding = mock_cls

    with patch.dict(sys.modules, {"fastembed": mock_fastembed}):
        try:
            emb.encode(["a"], DEFAULT)
            emb.encode(["b"], DEFAULT)
            emb.encode(["c"], DEFAULT)
        finally:
            emb._model = None
            emb._model_name_loaded = None

    assert mock_cls.call_count == 1


def _session_with(providers: list[str]) -> MagicMock:
    """Mock whose nested .model.model.get_providers() answers `providers`."""
    model = MagicMock()
    model.model.model.get_providers.return_value = providers
    return model


def test_cuda_not_requested_by_default(monkeypatch):
    """Unset PDF_MCP_CUDA loads the plain CPU model - unchanged behaviour."""
    import pdf_mcp.embedder as emb

    monkeypatch.delenv("PDF_MCP_CUDA", raising=False)
    cls = MagicMock(return_value=_session_with(["CPUExecutionProvider"]))
    try:
        with patch.dict(sys.modules, {"fastembed": MagicMock(TextEmbedding=cls)}):
            emb._get_model(DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    cls.assert_called_once_with(DEFAULT)


def test_cuda_requested_and_available(monkeypatch):
    """PDF_MCP_CUDA=1 asks fastembed for CUDA and keeps the session it gets."""
    import pdf_mcp.embedder as emb

    monkeypatch.setenv("PDF_MCP_CUDA", "1")
    gpu = _session_with(["CUDAExecutionProvider", "CPUExecutionProvider"])
    cls = MagicMock(return_value=gpu)
    try:
        with patch.dict(sys.modules, {"fastembed": MagicMock(TextEmbedding=cls)}):
            result = emb._get_model(DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    cls.assert_called_once_with(DEFAULT, cuda=True, device_ids=[0])
    assert result is gpu


def test_cuda_requested_but_cpu_given_warns(monkeypatch):
    """A CUDA request that silently lands on CPU warns and falls back.

    The failure this covers produces correct vectors, so only the clock shows
    it - which is why the warning is the behaviour under test.
    """
    import pdf_mcp.embedder as emb

    monkeypatch.setenv("PDF_MCP_CUDA", "1")
    cls = MagicMock(return_value=_session_with(["CPUExecutionProvider"]))
    try:
        with patch.dict(sys.modules, {"fastembed": MagicMock(TextEmbedding=cls)}):
            with pytest.warns(RuntimeWarning, match="gave CPU"):
                emb._get_model(DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    assert cls.call_count == 2  # the CUDA attempt, then the CPU fallback


def test_cuda_session_raising_falls_back(monkeypatch):
    """A CUDA session that cannot be built warns and still returns a model."""
    import pdf_mcp.embedder as emb

    monkeypatch.setenv("PDF_MCP_CUDA", "1")
    cpu = _session_with(["CPUExecutionProvider"])
    cls = MagicMock(side_effect=[RuntimeError("no provider"), cpu])
    try:
        with patch.dict(sys.modules, {"fastembed": MagicMock(TextEmbedding=cls)}):
            with pytest.warns(RuntimeWarning, match="CUDA session failed"):
                result = emb._get_model(DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    assert result is cpu


def test_cuda_session_that_cannot_encode_falls_back(monkeypatch):
    """A session that lists CUDA but fails its first encode falls back to CPU.

    get_providers() reports what was registered, not what will run: a cuDNN
    or cuBLAS series mismatch, a missing libcudnn, or an out-of-memory card
    all build a session that names CUDA and then raise on the first batch.
    The probe encode catches that at load time, while a CPU fallback is
    still possible, rather than inside a user's search.
    """
    import pdf_mcp.embedder as emb

    monkeypatch.setenv("PDF_MCP_CUDA", "1")
    broken = _session_with(["CUDAExecutionProvider", "CPUExecutionProvider"])
    broken.embed.side_effect = RuntimeError("CUDNN_STATUS_NOT_INITIALIZED")
    cpu = _session_with(["CPUExecutionProvider"])
    cls = MagicMock(side_effect=[broken, cpu])
    try:
        with patch.dict(sys.modules, {"fastembed": MagicMock(TextEmbedding=cls)}):
            with pytest.warns(RuntimeWarning, match="CUDNN_STATUS_NOT_INITIALIZED"):
                result = emb._get_model(DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    assert result is cpu
    assert cls.call_count == 2  # the CUDA attempt, then the CPU fallback


def test_cuda_probe_consumes_the_lazy_embed_generator(monkeypatch):
    """fastembed's embed() is a generator: nothing runs until it is iterated.

    A probe that only calls embed() would pass on every broken session, so
    the failure has to be raised from iteration, not from the call.
    """
    import pdf_mcp.embedder as emb

    def lazy_failure(texts):
        raise RuntimeError("CUBLAS_STATUS_ALLOC_FAILED")
        yield  # pragma: no cover - makes this a generator

    monkeypatch.setenv("PDF_MCP_CUDA", "1")
    broken = _session_with(["CUDAExecutionProvider", "CPUExecutionProvider"])
    broken.embed.side_effect = lazy_failure
    cpu = _session_with(["CPUExecutionProvider"])
    cls = MagicMock(side_effect=[broken, cpu])
    try:
        with patch.dict(sys.modules, {"fastembed": MagicMock(TextEmbedding=cls)}):
            with pytest.warns(RuntimeWarning, match="CUBLAS_STATUS_ALLOC_FAILED"):
                result = emb._get_model(DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    assert result is cpu


def test_cuda_probe_runs_one_short_encode(monkeypatch):
    """A working CUDA session is kept, and the probe costs one short text."""
    import pdf_mcp.embedder as emb

    monkeypatch.setenv("PDF_MCP_CUDA", "1")
    gpu = _session_with(["CUDAExecutionProvider", "CPUExecutionProvider"])
    cls = MagicMock(return_value=gpu)
    try:
        with patch.dict(sys.modules, {"fastembed": MagicMock(TextEmbedding=cls)}):
            result = emb._get_model(DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    assert result is gpu
    gpu.embed.assert_called_once()
    (texts,), _ = gpu.embed.call_args
    assert len(texts) == 1 and len(texts[0]) < 50


def test_cuda_requested_accepts_several_spellings(monkeypatch):
    """1, true, yes and on all mean yes; anything else means no."""
    import pdf_mcp.embedder as emb

    for value in ("1", "true", "TRUE", "yes", "on", " on "):
        monkeypatch.setenv("PDF_MCP_CUDA", value)
        assert emb._cuda_requested() is True, value
    for value in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv("PDF_MCP_CUDA", value)
        assert emb._cuda_requested() is False, value


def test_cuda_setting_three_states(monkeypatch):
    """on / off / None: 0 and off pin the CPU, unknown values mean unset."""
    import pdf_mcp.embedder as emb

    for value in ("0", "false", "OFF", " no "):
        monkeypatch.setenv("PDF_MCP_CUDA", value)
        assert emb._cuda_setting() == "off", value
    for value in ("", "maybe", "2"):
        monkeypatch.setenv("PDF_MCP_CUDA", value)
        assert emb._cuda_setting() is None, value
    monkeypatch.delenv("PDF_MCP_CUDA", raising=False)
    assert emb._cuda_setting() is None


def test_cuda_off_pins_the_cpu_provider(monkeypatch):
    """PDF_MCP_CUDA=0 passes cuda=False so fastembed cannot auto-pick the GPU.

    Unset is not enough on a machine with a system CUDA toolkit: fastembed's
    default is auto-detect and it took the GPU there (RESULTS.md finding 3).
    """
    import pdf_mcp.embedder as emb

    monkeypatch.setenv("PDF_MCP_CUDA", "0")
    cls = MagicMock(return_value=_session_with(["CPUExecutionProvider"]))
    try:
        with patch.dict(sys.modules, {"fastembed": MagicMock(TextEmbedding=cls)}):
            emb._get_model(DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None

    cls.assert_called_once_with(DEFAULT, cuda=False)


def test_providers_of_unreadable_session_is_empty():
    """A model that exposes no session reports no providers rather than raising."""
    import pdf_mcp.embedder as emb

    assert emb._providers(object()) == []


def test_preload_leaves_path_alone_off_windows(monkeypatch):
    """PATH is a Windows-only lever; Linux loads libraries instead.

    glob is patched so the test never opens a real library on a host that
    happens to have the nvidia wheels installed.
    """
    import glob
    import os
    import sys

    import pdf_mcp.embedder as emb

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(glob, "glob", lambda pattern, recursive=False: [])
    before = os.environ.get("PATH")
    emb._preload_cuda_runtime()
    assert os.environ.get("PATH") == before


def test_preload_registers_directories_on_windows(monkeypatch):
    """On Windows the wheel dirs go on PATH and into the DLL search path."""
    import glob
    import os
    import sys

    import pdf_mcp.embedder as emb

    dirs = [os.path.join("x", "nvidia", "cublas", "bin")]
    registered: list[str] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        glob, "glob", lambda pattern, recursive=False: [os.path.join(dirs[0], "a.dll")]
    )
    monkeypatch.setattr(os, "add_dll_directory", registered.append, raising=False)
    monkeypatch.setenv("PATH", "unchanged")

    emb._preload_cuda_runtime()

    assert registered == dirs
    assert os.environ["PATH"].startswith(dirs[0])


def test_preload_opens_libraries_on_linux(monkeypatch):
    """On Linux the libraries are opened directly - a directory cannot help.

    LD_LIBRARY_PATH is read by the dynamic loader before the interpreter
    exists, so the only lever left inside the process is loading each library
    with RTLD_GLOBAL.
    """
    import ctypes
    import glob
    import sys

    import pdf_mcp.embedder as emb

    libs = ["/x/nvidia/cublas/lib/libcublas.so.13", "/x/nvidia/bad/lib/b.so.1"]
    opened: list[str] = []

    def fake_cdll(path, mode=0):
        opened.append(path)
        if "bad" in path:
            raise OSError("cannot open")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(glob, "glob", lambda pattern, recursive=False: libs)
    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)

    emb._preload_cuda_runtime()

    # Both attempted, and the one that raised did not stop the other: which
    # libraries the provider needs depends on the build, so an unloadable one
    # is not fatal.
    assert sorted(opened) == sorted(libs)


def test_preload_loads_cublaslt_before_cublas(monkeypatch):
    """cublasLt goes first, which alphabetical order gets backwards.

    libcublas resolves libcublasLt through its own RUNPATH, so on a host that
    also has a system-wide CUDA it can bind a different version and fail later
    with missing symbols. Loading the wheel's copy first settles it - the same
    ordering, and the same reason, as PyTorch's _preload_cuda_deps.
    """
    import ctypes
    import glob
    import sys

    import pdf_mcp.embedder as emb

    libs = [
        "/x/nvidia/cudnn/lib/libcudnn.so.9",
        "/x/nvidia/cublas/lib/libcublas.so.13",
        "/x/nvidia/cublas/lib/libcublasLt.so.13",
    ]
    opened: list[str] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(glob, "glob", lambda pattern, recursive=False: libs)
    monkeypatch.setattr(ctypes, "CDLL", lambda path, mode=0: opened.append(path))

    emb._preload_cuda_runtime()

    assert opened[0].endswith("libcublasLt.so.13")
    assert opened[1].endswith("libcublas.so.13")


def test_preload_is_silent_when_no_wheels_are_installed(monkeypatch):
    """No nvidia wheels means nothing to do, on either platform."""
    import glob
    import os
    import sys

    import pdf_mcp.embedder as emb

    monkeypatch.setattr(glob, "glob", lambda pattern, recursive=False: [])
    monkeypatch.setenv("PATH", "unchanged")
    for platform in ("win32", "linux"):
        monkeypatch.setattr(sys, "platform", platform)
        emb._preload_cuda_runtime()
    assert os.environ["PATH"] == "unchanged"


# --- CPU batch composition -------------------------------------------------
# On a CPU session, encode() sorts texts by length and embeds in small
# sub-batches so onnxruntime pads each batch to a near neighbour instead of
# the longest text in the group (measured 2026-09-06: 1.37x, 2.8 GB -> 0.67 GB
# encode memory, vectors identical). A CUDA session keeps the single large
# batch, which is what a GPU wants.


def _recording_model(providers: list[str]) -> MagicMock:
    """Mock whose embed() returns a vector encoding each text's length, and
    records the calls it received, so order and batching are observable."""
    model = _session_with(providers)

    def embed(texts, **kwargs):
        model.calls.append((list(texts), kwargs))
        return (np.array([len(t), 1.0], dtype=np.float32) for t in texts)

    model.calls = []
    model.embed.side_effect = embed
    return model


def _with_model(model):
    import pdf_mcp.embedder as emb

    emb._model = model
    emb._model_name_loaded = DEFAULT
    return emb


def _reset():
    import pdf_mcp.embedder as emb

    emb._model = None
    emb._model_name_loaded = None


def test_cpu_encode_returns_vectors_in_input_order():
    """Sorting is internal: row i of the result is text i, whatever its length."""
    texts = ["a" * 300, "b" * 5, "c" * 120, "d" * 512, "e" * 40]
    model = _recording_model(["CPUExecutionProvider"])
    emb = _with_model(model)
    try:
        result = emb.encode(texts, DEFAULT)
    finally:
        _reset()

    # vector encodes the length; normalisation keeps the ratio between the
    # two components, so recover the length from it
    lengths = [round(v[0] / v[1]) for v in result]
    assert lengths == [len(t) for t in texts]


def test_cpu_encode_sorts_by_length_and_uses_small_batches():
    """The model sees texts shortest-first with the CPU batch size."""
    texts = ["a" * 300, "b" * 5, "c" * 120, "d" * 512, "e" * 40]
    model = _recording_model(["CPUExecutionProvider"])
    emb = _with_model(model)
    try:
        emb.encode(texts, DEFAULT)
    finally:
        _reset()

    assert len(model.calls) == 1
    seen, kwargs = model.calls[0]
    assert [len(t) for t in seen] == sorted(len(t) for t in texts)
    assert kwargs == {"batch_size": emb.CPU_BATCH_SIZE}
    assert emb.CPU_BATCH_SIZE == 16


def test_cuda_encode_keeps_single_unsorted_batch():
    """A CUDA session gets the texts as given, no sort, no batch_size: a GPU
    wants the large batch and this path is unchanged from before."""
    texts = ["a" * 300, "b" * 5, "c" * 120]
    model = _recording_model(["CUDAExecutionProvider", "CPUExecutionProvider"])
    emb = _with_model(model)
    try:
        result = emb.encode(texts, DEFAULT)
    finally:
        _reset()

    assert model.calls == [(texts, {})]
    assert [round(v[0] / v[1]) for v in result] == [300, 5, 120]


def test_cpu_encode_empty_list():
    model = _recording_model(["CPUExecutionProvider"])
    emb = _with_model(model)
    try:
        result = emb.encode([], DEFAULT)
    finally:
        _reset()

    assert result.size == 0
    assert model.calls == []
