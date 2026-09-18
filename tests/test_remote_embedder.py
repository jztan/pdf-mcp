"""Tests for pdf_mcp.remote_embedder.

All tests mock httpx.Client -- no network call, no live server. This repo
has no HTTP mocking library (see tests/test_url_fetcher.py); the pattern
here mirrors that file: patch("httpx.Client") and build a MagicMock
response with the attributes _post_with_retry actually reads
(status_code, json(), text, headers).

Scope note: this module only ever serves bge-small-en-v1.5 remotely (see
the module docstring in remote_embedder.py and issue #46), so there is no
document_prefix/query_prefix or dimensions surface to test here -- that
plumbing lives on the model-choice follow-up branch.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import numpy as np
import pytest

from pdf_mcp.remote_embedder import (
    RemoteEmbeddingError,
    RemoteSpec,
    _redact_base_url,
    encode,
)


def _spec(**overrides) -> RemoteSpec:
    defaults = dict(base_url="http://localhost:11434/v1", model="bge-small-en-v1.5")
    defaults.update(overrides)
    return RemoteSpec(**defaults)


def _mock_response(status_code=200, data=None, text="", headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text
    if data is not None:
        resp.json.return_value = data
    return resp


def _embeddings_body(vectors: list[list[float]], indices=None) -> dict:
    idx = indices if indices is not None else list(range(len(vectors)))
    return {"data": [{"index": i, "embedding": v} for i, v in zip(idx, vectors)]}


def _fake_vec(text: str, dim: int = 3) -> list[float]:
    """A deterministic, distinguishable vector for a given input text."""
    h = sum(text.encode("utf-8"))
    return [float((h + i) % 97) for i in range(dim)]


class TestRedactBaseUrl:
    def test_strips_userinfo(self):
        assert (
            _redact_base_url("http://user:secret@localhost:8000/v1")
            == "http://localhost:8000/v1"
        )

    def test_leaves_plain_url_unchanged(self):
        assert (
            _redact_base_url("http://localhost:8000/v1") == "http://localhost:8000/v1"
        )


class TestEncodeBasics:
    def test_empty_input_makes_no_request(self):
        with patch("httpx.Client") as mock_client:
            arr = encode([], _spec())
        mock_client.assert_not_called()
        assert arr.shape == (0,)

    def test_single_batch_round_trip(self):
        texts = ["alpha", "beta"]
        vectors = [_fake_vec(t) for t in texts]
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(data=_embeddings_body(vectors))
            arr = encode(texts, _spec(batch_size=32, max_concurrency=1))
        assert arr.shape == (2, 3)
        np.testing.assert_array_equal(arr, np.array(vectors, dtype=np.float32))

    def test_unnormalized_output(self):
        """embedder.py owns L2 normalization; this module must not."""
        texts = ["alpha"]
        vectors = [[3.0, 4.0, 0.0]]  # norm 5, deliberately not unit
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(data=_embeddings_body(vectors))
            arr = encode(texts, _spec())
        assert arr[0] == pytest.approx([3.0, 4.0, 0.0])

    def test_text_sent_unmodified(self):
        """No prefix surface in this narrow client -- input passes through."""
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(
                data=_embeddings_body([[1.0, 2.0]])
            )
            encode(["alpha"], _spec())
        sent = client.post.call_args.kwargs["json"]
        assert sent["input"] == ["alpha"]

    def test_requests_float_encoding_format(self):
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(data=_embeddings_body([[1.0]]))
            encode(["alpha"], _spec())
        sent = client.post.call_args.kwargs["json"]
        assert sent["encoding_format"] == "float"


class TestBatching:
    def test_splits_into_configured_batch_size(self):
        texts = [f"t{i}" for i in range(5)]
        calls = []

        def fake_post(url, json, headers):
            calls.append(list(json["input"]))
            vectors = [_fake_vec(t) for t in json["input"]]
            return _mock_response(data=_embeddings_body(vectors))

        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.side_effect = fake_post
            arr = encode(texts, _spec(batch_size=2, max_concurrency=1))

        assert [len(c) for c in calls] == [2, 2, 1]
        expected = np.array([_fake_vec(t) for t in texts], dtype=np.float32)
        np.testing.assert_array_equal(arr, expected)

    def test_order_preserved_across_concurrent_batches(self):
        """Batches complete out of order; the result must still be in the
        caller's input order (corpus._embed_doc_batched slices by
        position)."""
        texts = [f"t{i}" for i in range(8)]
        # Make later-submitted batches finish first, to actually exercise
        # reordering rather than coincidentally already being in order.
        delays = {0: 0.05, 1: 0.03, 2: 0.01, 3: 0.0}
        counter = {"n": 0}
        counter_lock = threading.Lock()

        def fake_post(url, json, headers):
            with counter_lock:
                call_idx = counter["n"]
                counter["n"] += 1
            time.sleep(delays.get(call_idx, 0.0))
            vectors = [_fake_vec(t) for t in json["input"]]
            return _mock_response(data=_embeddings_body(vectors))

        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.side_effect = fake_post
            arr = encode(texts, _spec(batch_size=2, max_concurrency=4))

        expected = np.array([_fake_vec(t) for t in texts], dtype=np.float32)
        np.testing.assert_array_equal(arr, expected)

    def test_shuffled_index_in_response_is_reordered(self):
        texts = ["a", "b", "c"]
        vectors = [_fake_vec(t) for t in texts]
        # Server returns them out of order; index field is authoritative.
        shuffled = [2, 0, 1]
        body = {"data": [{"index": i, "embedding": vectors[i]} for i in shuffled]}
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(data=body)
            arr = encode(texts, _spec(batch_size=32, max_concurrency=1))
        expected = np.array(vectors, dtype=np.float32)
        np.testing.assert_array_equal(arr, expected)

    def test_response_count_mismatch_raises(self):
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(
                data=_embeddings_body([[1.0, 2.0]])  # only 1, for 2 inputs
            )
            with pytest.raises(RemoteEmbeddingError, match="returned 1 vectors"):
                encode(["a", "b"], _spec(batch_size=32, max_concurrency=1))

    def test_ragged_rows_raise(self):
        body = {
            "data": [
                {"index": 0, "embedding": [1.0, 2.0]},
                {"index": 1, "embedding": [1.0, 2.0, 3.0]},
            ]
        }
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(data=body)
            with pytest.raises(RemoteEmbeddingError, match="inconsistent length"):
                encode(["a", "b"], _spec(batch_size=32, max_concurrency=1))


class TestRetries:
    def test_retries_on_429_honoring_retry_after(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(
            "pdf_mcp.remote_embedder.time.sleep", lambda s: sleeps.append(s)
        )
        responses = [
            _mock_response(
                status_code=429, text="rate limited", headers={"Retry-After": "0"}
            ),
            _mock_response(data=_embeddings_body([[1.0]])),
        ]
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.side_effect = responses
            arr = encode(["a"], _spec(batch_size=32, max_concurrency=1))
        assert arr.shape == (1, 1)
        assert client.post.call_count == 2
        assert 0.0 in sleeps  # the Retry-After value was honored

    def test_retries_on_5xx(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.remote_embedder.time.sleep", lambda s: None)
        responses = [
            _mock_response(status_code=503, text="unavailable"),
            _mock_response(data=_embeddings_body([[1.0]])),
        ]
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.side_effect = responses
            arr = encode(["a"], _spec(batch_size=32, max_concurrency=1))
        assert arr.shape == (1, 1)
        assert client.post.call_count == 2

    def test_no_retry_on_400(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.remote_embedder.time.sleep", lambda s: None)
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(
                status_code=400, text="bad request"
            )
            with pytest.raises(RemoteEmbeddingError, match="HTTP 400"):
                encode(["a"], _spec(batch_size=32, max_concurrency=1))
        assert client.post.call_count == 1

    def test_gives_up_after_max_attempts(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.remote_embedder.time.sleep", lambda s: None)
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(status_code=503, text="down")
            with pytest.raises(RemoteEmbeddingError, match="failed after 3 attempts"):
                encode(["a"], _spec(batch_size=32, max_concurrency=1))
        assert client.post.call_count == 3

    def test_retries_on_timeout(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.remote_embedder.time.sleep", lambda s: None)
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.side_effect = httpx.TimeoutException("timed out")
            with pytest.raises(RemoteEmbeddingError, match="failed after 3 attempts"):
                encode(["a"], _spec(batch_size=32, max_concurrency=1))
        assert client.post.call_count == 3

    def test_retries_on_connect_error(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.remote_embedder.time.sleep", lambda s: None)
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.side_effect = httpx.ConnectError("refused")
            with pytest.raises(RemoteEmbeddingError, match="failed after 3 attempts"):
                encode(["a"], _spec(batch_size=32, max_concurrency=1))
        assert client.post.call_count == 3

    def test_max_attempts_override_makes_a_single_try(self, monkeypatch):
        """remote_embedding_check.py sets max_attempts=1 so a startup check
        against an unreachable endpoint fails fast (PR #47 review item 3)
        instead of inheriting the 3-attempt bulk-embedding budget."""
        monkeypatch.setattr("pdf_mcp.remote_embedder.time.sleep", lambda s: None)
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.side_effect = httpx.ConnectError("refused")
            spec = _spec(batch_size=32, max_concurrency=1, max_attempts=1)
            with pytest.raises(RemoteEmbeddingError, match="failed after 1 attempts"):
                encode(["a"], spec)
        assert client.post.call_count == 1


class TestAuthAndSecrecy:
    def test_auth_header_present_when_api_key_set(self):
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(data=_embeddings_body([[1.0]]))
            encode(["a"], _spec(api_key="sk-super-secret"))
        headers = client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-super-secret"

    def test_auth_header_absent_when_no_api_key(self):
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(data=_embeddings_body([[1.0]]))
            encode(["a"], _spec(api_key=None))
        headers = client.post.call_args.kwargs["headers"]
        assert "Authorization" not in headers

    def test_api_key_absent_from_every_error_string(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.remote_embedder.time.sleep", lambda s: None)
        secret = "sk-super-secret-value"
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(status_code=500, text="oops")
            with pytest.raises(RemoteEmbeddingError) as exc_info:
                encode(["a"], _spec(api_key=secret, batch_size=32, max_concurrency=1))
        assert secret not in str(exc_info.value)

    def test_userinfo_in_base_url_not_in_error_string(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.remote_embedder.time.sleep", lambda s: None)
        with patch("httpx.Client") as mock_client:
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = _mock_response(status_code=500, text="oops")
            with pytest.raises(RemoteEmbeddingError) as exc_info:
                encode(
                    ["a"],
                    _spec(base_url="http://user:hunter2@localhost:9/v1"),
                )
        assert "hunter2" not in str(exc_info.value)
