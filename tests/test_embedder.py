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
        with pytest.raises(ImportError, match="pip install 'pdf-mcp\\[semantic\\]'"):
            emb.check_available(DEFAULT)


def _make_mock_model(dim: int = 384) -> MagicMock:
    """Mock fastembed TextEmbedding that yields dim-dimensional unit vectors."""
    mock = MagicMock()
    mock.embed.side_effect = lambda texts: (
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
            with pytest.raises(
                ImportError, match="pip install 'pdf-mcp\\[semantic\\]'"
            ):
                emb.encode(["hello"], DEFAULT)
    finally:
        emb._model = None
        emb._model_name_loaded = None


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
