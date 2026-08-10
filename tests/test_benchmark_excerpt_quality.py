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
            "answer_label": "Supply Voltage",
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
            "answer_label": "Param",
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
            "answer_label": "Param",
            "known_fail": {"cell": "paragraph", "reason": "  "},
        },
    )
    with pytest.raises(ValueError, match="known_fail"):
        load_queries(path)


def test_clause_1_ignores_frozen_rows():
    """Clause 1 measures the live corpus, matching clause 2's scope.

    Frozen rows score 0 on paragraph by definition and cannot get worse,
    so averaging them in only holds the gate red at a fixed offset.
    """
    rows = [
        _row("d01", 1, 0, known_fail=FROZEN),
        _row("d02", 1, 0, known_fail=FROZEN),
        _row("ok1", 0, 1),
        _row("ok2", 0, 1),
    ]
    verdict = evaluate_gate(_cells(paragraph=0.5, snippet=0.5), rows)
    c1 = verdict["clause_1_containment"]
    assert c1["pass"] is True
    assert c1["n_live"] == 2
    assert c1["paragraph"] == 1.0
    assert c1["snippet"] == 0.0


def test_clause_1_still_fails_on_a_live_regression():
    """Scoping to live rows must not make clause 1 unfailable."""
    rows = [_row("d01", 1, 0, known_fail=FROZEN), _row("ok1", 1, 0)]
    verdict = evaluate_gate(_cells(), rows)
    assert verdict["clause_1_containment"]["pass"] is False


def _one_page_pdf(tmp_path, text: str):
    """Write a real single-page PDF containing `text`."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 200), text, fontsize=11)
    path = tmp_path / "doc.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


def _corpus(path, page, answer):
    return {
        "doc": {
            "path": path,
            "title": "Doc",
            "queries": [
                {
                    "id": "q01",
                    "category": "table",
                    "query": "supply voltage",
                    "page": page,
                    "answer": answer,
                }
            ],
        }
    }


def test_run_all_cells_raises_when_answer_is_not_on_the_graded_page(tmp_path):
    """A drifted or wrong ground-truth page is a corpus error, not a 0.

    Without this the row scores 0 and reads as a quality regression, which
    is the same trap as a silently missing PDF one level up.
    """
    pdf = _one_page_pdf(tmp_path, "supply voltage is 4.5 volts minimum")
    corpus = _corpus(pdf, 1, "9.9")
    with pytest.raises(ValueError, match="q01"):
        run_all_cells(corpus)


def test_run_all_cells_raises_when_graded_page_is_out_of_range(tmp_path):
    """A page number past the end of a revised document must abort."""
    pdf = _one_page_pdf(tmp_path, "supply voltage is 4.5 volts minimum")
    corpus = _corpus(pdf, 7, "4.5")
    with pytest.raises(ValueError, match="q01"):
        run_all_cells(corpus)


def test_run_all_cells_accepts_an_answer_present_on_the_page(tmp_path):
    """The guard must not fire on a correctly graded query."""
    pdf = _one_page_pdf(tmp_path, "supply voltage is 4.5 volts minimum")
    cells, rows = run_all_cells(_corpus(pdf, 1, "4.5"))
    assert len(rows) == 1


def test_main_returns_2_on_a_drifted_ground_truth_page(tmp_path):
    """A corpus error exits 2 (setup), never 1 (quality regression)."""
    pdf = _one_page_pdf(tmp_path, "supply voltage is 4.5 volts minimum")
    qfile = tmp_path / "queries.json"
    qfile.write_text(json.dumps({"pdfs": _corpus(pdf, 1, "9.9")}))
    assert main(["--queries", str(qfile)]) == 2


def test_run_all_cells_raises_on_sha256_mismatch(tmp_path):
    """A swapped corpus file must abort, not silently re-score.

    Pinned bytes are what make baselines comparable over time; a file that
    changed underneath the gate reports as a quality change.
    """
    pdf = _one_page_pdf(tmp_path, "supply voltage is 4.5 volts minimum")
    corpus = _corpus(pdf, 1, "4.5")
    corpus["doc"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="sha256"):
        run_all_cells(corpus)


def test_run_all_cells_accepts_a_matching_sha256(tmp_path):
    """The correct digest passes and scoring proceeds."""
    import hashlib

    pdf = _one_page_pdf(tmp_path, "supply voltage is 4.5 volts minimum")
    corpus = _corpus(pdf, 1, "4.5")
    with open(pdf, "rb") as fh:
        corpus["doc"]["sha256"] = hashlib.sha256(fh.read()).hexdigest()
    _cells_out, rows = run_all_cells(corpus)
    assert len(rows) == 1


def test_table_answer_colliding_with_query_is_rejected(tmp_path):
    """A table answer inside its own query permits a false pass.

    x02 scored PASS for a while on 'VR = 25V, TJ = +150C' because answer
    "25" matched inside "VR = 25V" in a test-conditions block that carried
    no current value at all.
    """
    path = _query_file(
        tmp_path,
        {
            "id": "d01",
            "category": "table",
            "query": "peak reverse current at VR = 25V",
            "page": 1,
            "answer": "25",
            "answer_label": "Peak Reverse Current",
        },
    )
    with pytest.raises(ValueError, match="appears in the query"):
        load_queries(path)


def test_prose_answer_may_appear_in_its_query(tmp_path):
    """Prose answers are topic anchors, so overlap is expected, not a bug."""
    path = _query_file(
        tmp_path,
        {
            "id": "n05",
            "category": "prose",
            "query": "graph attention network",
            "page": 1,
            "answer": "attention",
        },
    )
    assert load_queries(path)["doc"]["queries"][0]["id"] == "n05"


def test_table_query_requires_answer_label(tmp_path):
    path = _query_file(
        tmp_path,
        {
            "id": "d01",
            "category": "table",
            "query": "supply voltage minimum",
            "page": 1,
            "answer": "4.5",
        },
    )
    with pytest.raises(ValueError, match="answer_label"):
        load_queries(path)


def test_interpretable_requires_column_identity_or_a_lone_number():
    """Containment cannot tell a usable answer from a bare number."""
    from scripts.benchmark_excerpt_quality import _is_interpretable

    # Three numbers, no column identity: which one is the minimum?
    assert _is_interpretable("Reset Voltage | 0.4 | 0.5 | 1 | V") is False
    # Position is not a substitute: the empty MIN cell is elided here.
    assert _is_interpretable("Threshold Current | (5) | 0.1 | 0.25") is False
    # A qualifier in the parameter name genuinely resolves it.
    assert _is_interpretable("Maximum instantaneous forward voltage 1.1 V") is True
    # A single number cannot be confused with anything.
    assert _is_interpretable("Total Capacitance CT pF") is True


def test_context_resolution_requires_a_single_number_in_the_cell():
    """TI's packed MIN cell must NOT count as resolved."""
    from scripts.benchmark_excerpt_quality import _resolves_via_context

    ti = {
        "table_context": {
            "header": ["PARAMETER", "TEST CONDITIONS", "MIN", "TYP", "MAX", "UNIT"],
            "rows": [["Reset Voltage", "", "0.4 0.5 1", "", "", "V"]],
            "columns_reliable": False,
        }
    }
    assert _resolves_via_context(ti, "0.4") is False


def test_context_resolution_accepts_a_clean_cell_under_a_named_column():
    from scripts.benchmark_excerpt_quality import _resolves_via_context

    esp = {
        "table_context": {
            "header": ["Parameter", "Description", "Min", "Max", "Unit"],
            "rows": [["Ioutput1", "Cumulative IO output current", "-", "1200", "mA"]],
            "columns_reliable": False,
        }
    }
    # columns_reliable is False and must NOT block resolution: the flag is
    # table-level, resolution is per value.
    assert _resolves_via_context(esp, "1200") is True


def test_context_resolution_rejects_an_unnamed_column():
    from scripts.benchmark_excerpt_quality import _resolves_via_context

    junk = {
        "table_context": {
            "header": ["Electrical Characteristics (@ TA = +25C)", "", "", ""],
            "rows": [["Reverse Recovery Time", "tRR", "4.0", "ns"]],
            "columns_reliable": True,
        }
    }
    assert _resolves_via_context(junk, "4.0") is False


def test_context_resolution_uses_number_tokens_not_substrings():
    """ "100" must not match inside "1000"."""
    from scripts.benchmark_excerpt_quality import _resolves_via_context

    m = {
        "table_context": {
            "header": ["Parameter", "Max", "Unit"],
            "rows": [["Leakage", "1000", "nA"]],
            "columns_reliable": True,
        }
    }
    assert _resolves_via_context(m, "100") is False


def test_context_resolution_accepts_a_fiscal_period_column_header():
    """A date header identifies a column as completely as MAX does.

    Starbucks 2025 p34 hands the caller 'Licensed stores | 4,350.4' under
    the header 'Sep 28, 2025'. The original vocabulary could not represent
    that as resolved, so no financial document could score above zero
    regardless of how well the code worked.
    """
    from scripts.benchmark_excerpt_quality import _resolves_via_context

    sbux = {
        "table_context": {
            "header": ["", "Sep 28,\n2025", "Sep 29,\n2024", "%\nChange"],
            "rows": [["Licensed stores", "4,350.4", "4,505.1", "(3.4"]],
            "columns_reliable": True,
        }
    }
    assert _resolves_via_context(sbux, "4,350.4") is True


def test_context_resolution_accepts_a_bare_year_column_header():
    from scripts.benchmark_excerpt_quality import _resolves_via_context

    brk = {
        "table_context": {
            "header": ["", "2024", "2023", "2022"],
            "rows": [["BNSF", "5,031", "5,087", "5,946"]],
            "columns_reliable": True,
        }
    }
    assert _resolves_via_context(brk, "5,031") is True


def test_context_resolution_still_rejects_an_unnamed_column_after_widening():
    """Widening to periods must not turn a blank header into a pass."""
    from scripts.benchmark_excerpt_quality import _resolves_via_context

    blank = {
        "table_context": {
            "header": ["", "", "", ""],
            "rows": [["Licensed stores", "4,350.4", "4,505.1", "(3.4"]],
            "columns_reliable": True,
        }
    }
    assert _resolves_via_context(blank, "4,350.4") is False


def test_context_resolution_still_rejects_ti_packed_cell_after_widening():
    """The TI rejection must survive the vocabulary change."""
    from scripts.benchmark_excerpt_quality import _resolves_via_context

    ti = {
        "table_context": {
            "header": ["PARAMETER", "TEST CONDITIONS", "MIN", "TYP", "MAX", "UNIT"],
            "rows": [["Reset Voltage", "", "0.4 0.5 1", "", "", "V"]],
            "columns_reliable": False,
        }
    }
    assert _resolves_via_context(ti, "0.4") is False


def test_context_resolution_looks_left_past_spacer_columns():
    """The label may sit left of its value when a spacer column intervenes.

    Berkshire p55: header ['', '2024', '', ...] over row
    ['BNSF', '', '5,031', ...]. Header and row are the same length; the
    currency column shifts the value one right of its own label. A reader
    resolves this instantly by looking left past the blank.
    """
    from scripts.benchmark_excerpt_quality import _resolves_via_context

    brk = {
        "table_context": {
            "header": ["", "2024", "", "", "", "2023", "", "", "", "2022", "", ""],
            "rows": [
                ["BNSF", "", "5,031", "", "", "", "5,087", "", "", "", "5,946", ""]
            ],
            "columns_reliable": True,
        }
    }
    assert _resolves_via_context(brk, "5,031") is True


def test_context_resolution_does_not_look_left_to_a_caption():
    """Looking left must not manufacture a label out of a section row."""
    from scripts.benchmark_excerpt_quality import _resolves_via_context

    sbux_bad = {
        "table_context": {
            "header": ["Net revenues:", "", "", "", "", "", "", ""],
            "rows": [
                [
                    "Store operating expenses",
                    "13,973.3",
                    "",
                    "12,467.1",
                    "",
                    "51.0",
                    "",
                    "46.2",
                ]
            ],
            "columns_reliable": True,
        }
    }
    assert _resolves_via_context(sbux_bad, "13,973.3") is False
