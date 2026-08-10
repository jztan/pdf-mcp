"""Tests for scripts/benchmark_excerpt_quality.py helpers."""

import json

import pymupdf
import pytest

from scripts.benchmark_excerpt_quality import (
    VALID_CATEGORIES,
    bbox_contains_answer,
    evaluate_gate,
    load_queries,
    main,
    run_all_cells,
)

GHOST_CORPUS = {
    "ghost": {
        "path": "/nonexistent/definitely_not_here.pdf",
        "title": "Ghost Document",
        "queries": [
            {
                "id": "x01",
                "category": "prose",
                "query": "anything",
                "page": 1,
                "answer": "anything",
            }
        ],
    }
}


def test_run_all_cells_raises_on_unresolvable_pdf():
    """A missing corpus PDF must abort, not silently score 0."""
    with pytest.raises(FileNotFoundError, match="definitely_not_here.pdf"):
        run_all_cells(GHOST_CORPUS)


def test_main_returns_2_on_unresolvable_pdf(tmp_path):
    """The abort surfaces as exit code 2 (setup error), not 0 or 1."""
    qfile = tmp_path / "queries.json"
    qfile.write_text(json.dumps({"pdfs": GHOST_CORPUS}))
    assert main(["--queries", str(qfile)]) == 2


def test_bbox_contains_answer_true_and_false():
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(
        (72, 200), "the answer phrase lives here in this block.", fontsize=11
    )
    blocks = [b for b in page.get_text("blocks", sort=True) if b[6] == 0]
    bbox = list(blocks[0][:4])
    assert bbox_contains_answer(page, bbox, "answer phrase") is True
    assert bbox_contains_answer(page, [0, 0, 5, 5], "answer phrase") is False
    doc.close()


def test_bbox_contains_answer_normalizes_hyphens_and_case():
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(
        (72, 200), "In-Context Learning is a prompting technique.", fontsize=11
    )
    blocks = [b for b in page.get_text("blocks", sort=True) if b[6] == 0]
    bbox = list(blocks[0][:4])
    assert bbox_contains_answer(page, bbox, "in context learning") is True
    doc.close()


def _query_file(tmp_path, query: dict):
    """Write a one-query corpus file and return its path."""
    qfile = tmp_path / "q.json"
    qfile.write_text(
        json.dumps(
            {
                "pdfs": {
                    "doc": {
                        "url": "https://example.invalid/x.pdf",
                        "title": "Doc",
                        "queries": [query],
                    }
                }
            }
        )
    )
    return str(qfile)


def test_table_category_is_valid(tmp_path):
    """A query in the table class loads without a schema error."""
    path = _query_file(
        tmp_path,
        {
            "id": "d01",
            "category": "table",
            "query": "supply voltage",
            "page": 5,
            "answer": "4.5",
        },
    )
    loaded = load_queries(path)
    assert loaded["doc"]["queries"][0]["category"] == "table"
    assert "table" in VALID_CATEGORIES


def _cells(paragraph=1.0, snippet=1.0, bbox=1.0):
    return {
        "snippet": {"all": snippet},
        "paragraph": {"all": paragraph},
        "bbox": {"all": bbox},
    }


def _row(rid, snip, para, known_fail=None, bbox_present=1, bbox_contains=1):
    return {
        "id": rid,
        "snippet_contains": snip,
        "paragraph_contains": para,
        "bbox_present": bbox_present,
        "bbox_contains": bbox_contains,
        "known_fail": known_fail,
    }


FROZEN = {"cell": "paragraph", "reason": "long-block win"}


def test_known_fail_row_does_not_trip_clause_2():
    """A frozen paragraph failure is documented, not a gate failure."""
    verdict = evaluate_gate(_cells(), [_row("d01", 1, 0, known_fail=FROZEN)])
    assert verdict["clause_2_regressions"]["pass"] is True
    assert verdict["clause_2_regressions"]["count"] == 0


def test_unfrozen_regression_still_trips_clause_2():
    """A new regression fails even when other rows are frozen."""
    rows = [_row("d01", 1, 0, known_fail=FROZEN), _row("d02", 1, 0)]
    verdict = evaluate_gate(_cells(), rows)
    assert verdict["clause_2_regressions"]["pass"] is False
    assert verdict["clause_2_regressions"]["ids"] == ["d02"]


def test_known_fail_that_now_passes_fails_the_gate():
    """The ratchet only tightens: a fixed row must be un-frozen."""
    verdict = evaluate_gate(_cells(), [_row("d01", 1, 1, known_fail=FROZEN)])
    assert verdict["clause_4_stale_known_fail"]["pass"] is False
    assert verdict["clause_4_stale_known_fail"]["ids"] == ["d01"]
    assert verdict["pass"] is False


def test_clean_corpus_passes_all_four_clauses():
    verdict = evaluate_gate(_cells(), [_row("d01", 1, 1), _row("d02", 0, 1)])
    assert verdict["pass"] is True


def test_load_queries_rejects_malformed_known_fail(tmp_path):
    """A frozen failure must name a real cell and carry a reason."""
    path = _query_file(
        tmp_path,
        {
            "id": "d01",
            "category": "table",
            "query": "q",
            "page": 1,
            "answer": "a",
            "known_fail": {"cell": "bogus", "reason": "x"},
        },
    )
    with pytest.raises(ValueError, match="known_fail"):
        load_queries(path)


def test_load_queries_rejects_known_fail_without_reason(tmp_path):
    """An unexplained frozen failure is indistinguishable from hiding one."""
    path = _query_file(
        tmp_path,
        {
            "id": "d01",
            "category": "table",
            "query": "q",
            "page": 1,
            "answer": "a",
            "known_fail": {"cell": "paragraph", "reason": "  "},
        },
    )
    with pytest.raises(ValueError, match="known_fail"):
        load_queries(path)
