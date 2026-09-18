"""
HTTP client for a self-hosted, OpenAI-compatible ``POST /v1/embeddings``
endpoint.

Covers ollama, lemonade, bare ``llama-server``, and vLLM with one
implementation, since they all speak the same request/response schema. This
module is intentionally the only place that touches the network for
embeddings; ``embedder.py`` owns dispatch and normalization, ``config.py``
owns parsing ``[embedding]`` into a `RemoteSpec`.

Scope (see issue #42 and issue #46): this client only ever serves
BAAI/bge-small-en-v1.5 remotely -- the point is a Vulkan/iGPU speed win on
hardware fastembed's onnxruntime backends can't reach, NOT arbitrary model
choice. `RemoteSpec.model` exists solely for cache-identity naming (so a
config change that points at a different remote model still gets its own
cache rows); it is not a signal that changes how text is encoded. Nothing
here applies a document/query prefix or validates a `dimensions` value --
bge-small needs neither. General model support (arbitrary prefixes,
dimension validation, and the low_confidence/RRF threshold recalibration
that a different model's cosine distribution would require) is tracked
separately in issue #46 and lives on top of this in a follow-up branch.

Unlike the local fastembed path (one process, one encode call), a remote
endpoint is I/O-bound: batches are issued concurrently from a bounded thread
pool (``httpx.Client`` is thread-safe; `_get_model` in embedder.py is
explicitly documented as NOT thread-safe, which is why this stays a separate
client rather than a shared module global).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import numpy as np

# Default attempts per batch: 1 initial + up to 2 retries. A prewarm run is
# thousands of requests against a server that may be a personal machine
# (rate limits, a cold model load, a GPU busy with something else) -- worth
# more resilience than url_fetcher.py's single bare retry (that one guards
# against one transient download corruption, not a sustained multi-minute
# run). `RemoteSpec.max_attempts` lets a caller override this -- see its
# docstring and remote_embedding_check.py, which wants exactly one attempt
# so an unreachable endpoint fails fast at startup instead of inheriting
# this budget.
MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.5  # seconds, doubled each attempt, plus jitter
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RemoteEmbeddingError(RuntimeError):
    """The remote endpoint could not be used to produce embeddings."""


@dataclass(frozen=True)
class RemoteSpec:
    """Everything needed to call one OpenAI-compatible embeddings endpoint.

    ``api_key`` is the resolved secret value (already read from the env var
    named by ``[embedding].api_key_env``); this dataclass is never logged or
    included in an exception message in full -- see `_redact_base_url` and
    the header-only use in `_headers`.

    ``model`` is informational only -- it names which model the remote
    server should load, but nothing in this module or in embedder.py
    branches on its value, and it is NOT folded into the vector-cache
    identity (see PDFConfig.embedding_model's docstring: a verified remote
    endpoint deliberately shares cache rows with local fastembed). There is
    deliberately no `dimensions`, `document_prefix`, or `query_prefix`
    field: this version only ever talks to bge-small-en-v1.5, which needs
    none of them. See the module docstring and issue #46.

    ``max_attempts`` overrides `MAX_ATTEMPTS` for this spec -- used by
    remote_embedding_check.py to make exactly one attempt at startup rather
    than inheriting the bulk-embedding retry budget.
    """

    base_url: str
    model: str
    api_key: "str | None" = None
    timeout: float = 60.0
    batch_size: int = 32
    max_concurrency: int = 4
    max_attempts: int = MAX_ATTEMPTS


def _redact_base_url(base_url: str) -> str:
    """Strip userinfo (user:pass@) before a URL ever reaches a log or error
    message -- a base_url is user config and could embed credentials."""
    parts = urlsplit(base_url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _headers(spec: RemoteSpec) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if spec.api_key:
        headers["Authorization"] = f"Bearer {spec.api_key}"
    return headers


def _endpoint_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/embeddings"


def _post_with_retry(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    max_attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any]:
    """POST with retry on connect errors, timeouts, 429 and 5xx.

    `max_attempts` defaults to the bulk-embedding budget (`MAX_ATTEMPTS`)
    but is overridden per-spec (`RemoteSpec.max_attempts`) -- e.g.
    remote_embedding_check.py wants exactly 1, so a startup check fails
    fast on an unreachable endpoint instead of blocking for minutes.

    Never includes the Authorization header's value in a raised message --
    only the redacted URL and the response body/status, so a leaked
    exception (logs, an MCP error payload) cannot carry the API key.
    """
    last_exc: "Exception | None" = None
    for attempt in range(max_attempts):
        try:
            resp = client.post(url, json=payload, headers=headers)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
        else:
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
            if resp.status_code not in _RETRYABLE_STATUS:
                raise RemoteEmbeddingError(
                    f"embedding request to {_redact_base_url(url)} failed: "
                    f"HTTP {resp.status_code}: {resp.text[:500]}"
                )
            last_exc = RemoteEmbeddingError(
                f"HTTP {resp.status_code}: {resp.text[:500]}"
            )
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None and attempt < max_attempts - 1:
                try:
                    time.sleep(max(0.0, float(retry_after)))
                    continue
                except ValueError:
                    pass  # not a numeric seconds value; fall through to backoff
        if attempt < max_attempts - 1:
            delay = _RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 0.25)
            time.sleep(delay)
    raise RemoteEmbeddingError(
        f"embedding request to {_redact_base_url(url)} failed after "
        f"{max_attempts} attempts: {last_exc!r}"
    )


def _embed_batch(
    client: httpx.Client, spec: RemoteSpec, texts: list[str]
) -> list[list[float]]:
    """One request for one batch. Returns rows in the CALLER's order.

    The OpenAI schema's `data[]` carries an `index` per item and does not
    guarantee response order matches request order -- reorder by it rather
    than trusting array position, and verify the count matches the batch
    (a server silently dropping an oversized/empty input would otherwise
    desync corpus.py's per-page unit slicing, which relies on positional
    alignment).
    """
    payload: dict[str, Any] = {
        "model": spec.model,
        "input": texts,
        # Several servers (notably some OpenAI-compatible proxies) default
        # to base64; this codebase's whole read path is
        # np.frombuffer(blob, dtype=np.float32) with no header, so a
        # silently base64-or-float64 response would be misread as garbage
        # rather than fail loudly. Ask for floats explicitly.
        "encoding_format": "float",
    }
    body = _post_with_retry(
        client,
        _endpoint_url(spec.base_url),
        payload,
        _headers(spec),
        max_attempts=spec.max_attempts,
    )
    data = body.get("data")
    if not isinstance(data, list):
        raise RemoteEmbeddingError(
            f"embedding response from {_redact_base_url(spec.base_url)} "
            f"has no usable 'data' list: {str(body)[:300]}"
        )
    if len(data) != len(texts):
        raise RemoteEmbeddingError(
            f"embedding response returned {len(data)} vectors for "
            f"{len(texts)} inputs (model={spec.model!r})"
        )
    try:
        ordered = sorted(data, key=lambda item: item["index"])
        rows: list[list[float]] = [item["embedding"] for item in ordered]
    except (KeyError, TypeError) as exc:
        raise RemoteEmbeddingError(
            f"embedding response from {_redact_base_url(spec.base_url)} "
            f"is missing 'index'/'embedding' fields: {exc!r}"
        ) from exc
    return rows


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def encode(texts: list[str], spec: RemoteSpec) -> Any:
    """
    Encode `texts` via the OpenAI-compatible endpoint in `spec`.

    Returns an ndarray of shape (N, D), dtype float32, UNNORMALIZED --
    embedder.py owns L2 normalization for both the local and remote paths
    (servers differ in whether they return unit vectors, same reasoning as
    the fastembed path).

    # TODO(model-choice, see issue #46): this is the natural extension
    # point for an asymmetric document/query prefix contract (nomic,
    # Qwen3-Embedding, etc. all need one) and for validating a configured
    # `dimensions` against the response. Both are deliberately absent here:
    # this client only ever serves bge-small-en-v1.5, which needs neither,
    # and adding either back also means recalibrating the low_confidence /
    # hybrid RRF fusion thresholds tuned to bge-small's cosine distribution
    # -- see the model-choice branch for that follow-up work.
    """
    if not texts:
        return np.empty((0,), dtype=np.float32)

    batches = _chunks(texts, max(1, spec.batch_size))
    results: "list[list[list[float]] | None]" = [None] * len(batches)

    if len(batches) == 1 or spec.max_concurrency <= 1:
        with httpx.Client(timeout=spec.timeout) as client:
            for i, batch in enumerate(batches):
                results[i] = _embed_batch(client, spec, batch)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with httpx.Client(timeout=spec.timeout) as client:
            with ThreadPoolExecutor(
                max_workers=min(spec.max_concurrency, len(batches))
            ) as pool:
                futures = {
                    pool.submit(_embed_batch, client, spec, batch): i
                    for i, batch in enumerate(batches)
                }
                for fut in as_completed(futures):
                    # Order preservation is contract-critical here too:
                    # results[i] keyed by submission index, not completion
                    # order, then flattened below in that same order.
                    results[futures[fut]] = fut.result()

    flat_rows: list[list[float]] = []
    for batch_rows in results:
        assert batch_rows is not None  # every future/branch above fills its slot
        flat_rows.extend(batch_rows)

    try:
        arr = np.array(flat_rows, dtype=np.float32)
    except ValueError as exc:
        # numpy>=1.24 raises rather than silently building an object array
        # when row lengths differ -- e.g. one server response mixed
        # dimensions mid-batch. Surface it as our own error type instead of
        # a bare numpy ValueError so it matches every other failure mode
        # here.
        raise RemoteEmbeddingError(
            f"embedding response from {_redact_base_url(spec.base_url)} "
            f"returned vectors of inconsistent length: {exc}"
        ) from exc
    if arr.ndim != 2:
        raise RemoteEmbeddingError(
            f"embedding response from {_redact_base_url(spec.base_url)} "
            f"produced ragged/empty vectors (shape={arr.shape})"
        )
    return arr
