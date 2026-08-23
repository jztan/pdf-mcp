"""Corpus fixtures for backend differential tests."""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_CORPORA = {
    "content_trust": _ROOT / "benchmark_data/content_trust_corpus",
    "charts": _ROOT / "benchmark_data/chart_extraction/syn_corpus",
    "legend_attacks": _ROOT / "benchmark_data/chart_extraction/legend_attacks",
    "reading_order": _ROOT / "benchmark_data/.reading_order_pdfs",
    "financial": _ROOT / "docs_internal/sample_pdfs/financial",
    "cjk": _ROOT / "docs_internal/sample_pdfs/vertical-jp",
    "pages": _ROOT / "pages/corpus",
}


@pytest.fixture
def corpus_pdfs():
    """Return sorted PDFs for a named corpus, skipping if absent.

    reading_order, financial and cjk are local-only and gitignored, so a
    clean checkout skips those tests rather than failing.
    """

    def _get(kind: str) -> list[Path]:
        directory = _CORPORA[kind]
        if not directory.exists():
            pytest.skip(f"corpus {kind} not present at {directory}")
        pdfs = sorted(directory.glob("*.pdf"))
        if not pdfs:
            pytest.skip(f"corpus {kind} is empty")
        return pdfs

    return _get
