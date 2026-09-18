"""In-process cache of per-document embedding matrices for semantic scoring.

The SQLite cache stores one blob per embedding unit. Scoring a corpus by
decoding every blob on every query costs tens of thousands of small numpy
allocations and Python iterations per query; the same arithmetic as one
matrix-vector product per document is milliseconds. This module stacks a
document's units once per process (keyed on path, mtime, model and
extraction version, which is the cache's own validity rule) and bounds the
total by bytes, so a large corpus degrades to reloading, never to unbounded
memory. STDIO servers live for one conversation, so the cache does too.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

_DEFAULT_MB = 256


def _max_bytes_from_env() -> int:
    """Budget in bytes from PDF_MCP_VECTOR_CACHE_MB; 0 disables caching."""
    raw = os.environ.get("PDF_MCP_VECTOR_CACHE_MB", "").strip()
    if not raw:
        return _DEFAULT_MB * 1024 * 1024
    try:
        return max(0, int(raw)) * 1024 * 1024
    except ValueError:
        return _DEFAULT_MB * 1024 * 1024


@dataclass
class DocMatrix:
    M: Any  # (n_units, dim) float32, C-contiguous
    offsets: Any  # int64, start row of each page in `pages`
    pages: list[int]  # 0-indexed page numbers, ascending

    @property
    def nbytes(self) -> int:
        return int(self.M.nbytes)


def build_doc_matrix(blobs_by_page: dict[int, list[bytes]]) -> DocMatrix | None:
    """Stack a document's unit vectors page by page. Pages with no blob are
    skipped; None when no page has one."""
    pages = [p for p in sorted(blobs_by_page) if blobs_by_page[p]]
    if not pages:
        return None
    rows: list[Any] = []
    offsets = np.empty(len(pages), dtype=np.int64)
    n = 0
    for i, p in enumerate(pages):
        offsets[i] = n
        for b in blobs_by_page[p]:
            rows.append(np.frombuffer(b, dtype=np.float32))
            n += 1
    M = np.ascontiguousarray(np.stack(rows).astype(np.float32, copy=False))
    return DocMatrix(M=M, offsets=offsets, pages=pages)


def score_doc(dm: DocMatrix, query_vec: Any) -> tuple[Any, Any]:
    """(page_max, best_unit_local): the max cosine per page in `dm.pages`
    order, and the argmax unit index within each page (0 = whole page)."""
    sims = dm.M @ np.asarray(query_vec, dtype=np.float32)
    page_max = np.maximum.reduceat(sims, dm.offsets)
    ends = np.append(dm.offsets[1:], len(sims))
    best = np.empty(len(dm.pages), dtype=np.int64)
    for i, (lo, hi) in enumerate(zip(dm.offsets, ends)):
        best[i] = int(np.argmax(sims[lo:hi]))
    return page_max, best


def page_max_from_lists(
    embeddings: dict[int, list[Any]], query_vec: Any
) -> tuple[list[int], Any]:
    """Per-page max cosine over already-decoded vectors, pages ascending.
    One stacked product instead of a Python max per page; pages with no
    vectors are skipped rather than raising on max([])."""
    pages = sorted(p for p in embeddings if len(embeddings[p]))
    if not pages:
        return [], np.zeros(0, dtype=np.float32)
    counts = [len(embeddings[p]) for p in pages]
    dm = DocMatrix(
        M=np.ascontiguousarray(
            np.stack([v for p in pages for v in embeddings[p]]).astype(
                np.float32, copy=False
            )
        ),
        offsets=np.cumsum([0] + counts[:-1]).astype(np.int64),
        pages=pages,
    )
    page_max, _best = score_doc(dm, query_vec)
    return pages, page_max


class MatrixCache:
    """LRU by bytes. `get` returns the loader's result even when it cannot be
    stored (budget 0, or a single matrix above the budget)."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(0, int(max_bytes))
        self._d: OrderedDict[tuple[Any, ...], DocMatrix] = OrderedDict()
        self._bytes = 0
        self._hits = 0
        self._misses = 0

    def get(
        self, key: tuple[Any, ...], loader: Callable[[], DocMatrix | None]
    ) -> DocMatrix | None:
        dm = self._d.get(key)
        if dm is not None:
            self._hits += 1
            self._d.move_to_end(key)
            return dm
        self._misses += 1
        dm = loader()
        if dm is None or self.max_bytes == 0 or dm.nbytes > self.max_bytes:
            return dm
        self._d[key] = dm
        self._bytes += dm.nbytes
        while self._bytes > self.max_bytes and self._d:
            _k, old = self._d.popitem(last=False)
            self._bytes -= old.nbytes
        return dm

    def clear(self) -> None:
        self._d.clear()
        self._bytes = 0

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._d),
            "bytes": self._bytes,
            "hits": self._hits,
            "misses": self._misses,
        }


CACHE = MatrixCache(_max_bytes_from_env())
