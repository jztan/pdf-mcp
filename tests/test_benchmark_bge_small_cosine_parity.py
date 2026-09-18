"""Tests for scripts/benchmark_bge_small_cosine_parity.py.

Two tiers:
  - fast/offline: the corpus-chunking half needs no network and always
    runs (real pages/corpus/*.pdf are checked into the repo).
  - slow: the full cosine-parity run against a live `llama-server` --
    skipped whenever nothing answers DEFAULT_BASE_URL, since CI has no
    GPU/Vulkan server to point at (see the module docstring's launch
    command for how to run one locally).
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import benchmark_bge_small_cosine_parity as bench  # noqa: E402


def server_available(base_url: str = bench.DEFAULT_BASE_URL) -> bool:
    try:
        root = base_url.rsplit("/v1", 1)[0] if base_url.endswith("/v1") else base_url
        resp = httpx.get(root.rstrip("/") + "/health", timeout=1.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


class TestCorpusChunking:
    """No network involved -- exercises the real extractor chunking path
    against the real checked-in corpus."""

    def test_corpus_pdfs_exist(self):
        assert bench.CORPUS_DIR.exists()
        assert list(bench.CORPUS_DIR.glob("*.pdf"))

    def test_extractor_chunking_produces_real_windows(self):
        """Sanity check on the non-network half of collect_passages: real
        page text really does produce ~300-token chunk_page_text windows
        across multiple corpus PDFs, independent of the server-side token
        filter."""
        from pdf_mcp.docopen import open_pdf
        from pdf_mcp.extractor import chunk_page_text, extract_text_from_page

        n_chunks = 0
        n_pdfs = 0
        for pdf_path in sorted(bench.CORPUS_DIR.glob("*.pdf")):
            doc = open_pdf(str(pdf_path))
            n_pdfs += 1
            for pn in range(len(doc)):
                text = extract_text_from_page(doc[pn])
                if text and text.strip():
                    n_chunks += len(chunk_page_text(text))
        assert n_pdfs >= 5
        assert n_chunks >= 30


@pytest.mark.slow
@pytest.mark.skipif(not server_available(), reason="no live llama-server reachable")
class TestLiveCosineParity:
    def test_min_cosine_clears_default_threshold(self):
        result = bench.run(
            bench.DEFAULT_BASE_URL,
            "bge-small-en-v1.5-q8_0",
            bench.DEFAULT_THRESHOLD,
            bench.MAX_CHUNKS_PER_PDF,
        )
        assert result["n_passages"] >= 20
        assert result["min_cosine"] >= bench.DEFAULT_THRESHOLD
        assert result["verdict"] == "PASS"
