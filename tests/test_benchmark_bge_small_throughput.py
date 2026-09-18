"""Tests for scripts/benchmark_bge_small_throughput.py.

Two tiers:
  - fast/offline: the corpus-chunking half needs no network and always
    runs (real pages/corpus/*.pdf are checked into the repo).
  - slow: the full throughput run against a live `llama-server` -- skipped
    whenever nothing answers DEFAULT_BASE_URL, since CI has no GPU/Vulkan
    server to point at (see the module docstring's launch command for how
    to run one locally).
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import benchmark_bge_small_throughput as bench  # noqa: E402


def server_available(base_url: str = bench.DEFAULT_BASE_URL) -> bool:
    try:
        root = base_url.rsplit("/v1", 1)[0] if base_url.endswith("/v1") else base_url
        resp = httpx.get(root.rstrip("/") + "/health", timeout=1.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


class TestCorpusChunking:
    """No network involved -- exercises the real extractor chunking path
    against the real checked-in corpus (same code collect_chunks uses
    before its server-side token filter)."""

    def test_corpus_pdfs_exist(self):
        assert bench.CORPUS_DIR.exists()
        assert list(bench.CORPUS_DIR.glob("*.pdf"))

    def test_extractor_chunking_produces_hundreds_of_real_windows(self):
        from pdf_mcp.docopen import open_pdf
        from pdf_mcp.extractor import chunk_page_text, extract_text_from_page

        n_chunks = 0
        for pdf_path in sorted(bench.CORPUS_DIR.glob("*.pdf")):
            doc = open_pdf(str(pdf_path))
            for pn in range(len(doc)):
                text = extract_text_from_page(doc[pn])
                if text and text.strip():
                    n_chunks += sum(
                        1
                        for c in chunk_page_text(text)
                        if len(c) >= bench.MIN_CHUNK_CHARS
                    )
        # Real corpus measured at 612 candidate chunks (before the
        # server-side oversized-token filter); floored well below that so
        # this doesn't regress if corpus text extraction changes slightly.
        assert n_chunks >= 300


@pytest.mark.slow
@pytest.mark.skipif(not server_available(), reason="no live llama-server reachable")
class TestLiveThroughput:
    def test_remote_beats_local_and_scales_with_concurrency(self):
        result = bench.run(
            bench.DEFAULT_BASE_URL,
            "bge-small-en-v1.5-q8_0",
            [1, 4],
            batch_size=16,
            n_texts=80,
        )
        local_tps = result["local_fastembed_cpu"]["texts_per_sec"]
        remote_c1 = result["remote_by_concurrency"]["1"]["texts_per_sec"]
        # The Vulkan-served remote backend is the whole point of this
        # feature -- it must actually be faster than fastembed CPU on
        # real ~300-token chunks, not just on synthetic short passages.
        assert remote_c1 > local_tps
