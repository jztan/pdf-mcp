"""Hidden-text flags on real PDFs, pinned page by page.

The synthetic content-trust corpus passed for the whole 3.0.0-3.4.0 range
while the detector flagged 1,092 pages of 27 real filings, nearly all
visible text (2026-10-01 sweep). These tests pin the detector's output on
the public-domain PDFs committed under pages/corpus, so CI fails on any
page that starts or stops being flagged. The local real-PDF half (10-Ks,
Starbucks, BGB) is scripts/benchmark_hidden_text.py.
"""

from pathlib import Path

import pytest

from scripts.benchmark_hidden_text import (
    COMMITTED,
    ROOT,
    compare,
    load_baseline,
    scan_pdf,
)


def test_committed_corpus_is_present_and_baselined():
    """A vacuity guard: an empty glob or an unbaselined new PDF would make
    the parametrized test below pass by running nothing."""
    assert len(COMMITTED) >= 6
    docs = load_baseline()["docs"]
    missing = [rel for rel in COMMITTED if rel not in docs]
    assert not missing, f"run --update-baseline for {missing}"


@pytest.mark.parametrize("rel", COMMITTED)
def test_committed_pdf_flags_match_baseline(rel):
    base = load_baseline()["docs"][rel]
    got = scan_pdf(Path(ROOT) / rel)
    assert got["sha256"] == base["sha256"], f"{rel} changed on disk"
    assert (
        got["flagged"] == base["flagged"]
    ), f"{rel}: flagged pages {got['flagged']} != baseline {base['flagged']}"


def test_compare_reports_new_and_cleared_pages():
    base = {"docs": {"a.pdf": {"flagged": [1, 2]}, "b.pdf": {"flagged": []}}}
    results = {"a.pdf": {"flagged": [2, 5]}, "b.pdf": {"flagged": []}}
    regressions, improvements = compare(base, results)
    assert regressions == {"a.pdf": [5]}
    assert improvements == {"a.pdf": [1]}
