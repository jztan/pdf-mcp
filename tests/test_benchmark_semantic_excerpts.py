"""Pure-logic tests for the pure-semantic excerpt ratchet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.benchmark_semantic_excerpts import (
    evaluate_ratchet,
    main,
    render_markdown,
    summarize,
)


def _corpus_rows(values: dict[str, float], cls: str = "needle") -> dict:
    return {
        q: {
            "class": cls,
            "span_recall": v,
            "doc_ndcg": 0.8,
            "realized_k": 5,
            "excerpt_digest": f"d-{q}-{v}",
        }
        for q, v in values.items()
    }


def _single_rows(values: dict[str, int], cls: str = "prose") -> dict:
    return {
        q: {
            "class": cls,
            "page_hit": 1,
            "contains": v,
            "excerpt_digest": f"d-{q}-{v}",
        }
        for q, v in values.items()
    }


def _run(corpus: dict | None = None, single: dict | None = None) -> dict:
    arms = {}
    if corpus is not None:
        arms["corpus"] = {
            "rows": {"snippet": corpus, "paragraph": corpus},
            "seconds_per_query": {"snippet": 1.0, "paragraph": 1.5},
        }
    if single is not None:
        arms["single"] = {
            "rows": {"snippet": single, "paragraph": single},
            "seconds_per_query": {"snippet": 0.1, "paragraph": 0.1},
        }
    return {
        "generated": "2026-09-05T00:00:00+00:00",
        "git_head": "abc1234",
        "config": {
            "corpus_top_k": 25,
            "corpus_budget_tokens": 2000,
            "single_max_results": 5,
            "limit": None,
        },
        "arms": arms,
    }


N = 40
IDS = [f"q{i:02d}" for i in range(N)]


class TestEvaluateRatchet:
    def test_identical_runs_pass_with_no_changed_excerpts(self):
        run = _run(single=_single_rows({q: 1 for q in IDS}))
        v = evaluate_ratchet(run, run)
        assert v["pass"] is True
        assert v["regressions"] == [] and v["improvements"] == []
        assert all(
            c["changed_excerpts"] == 0 for c in v["cells"] if c["class"] == "all"
        )

    def test_class_wide_drop_fails(self):
        base = _run(single=_single_rows({q: 1 for q in IDS}))
        cur = _run(single=_single_rows({q: 0 for q in IDS}))
        v = evaluate_ratchet(base, cur)
        assert v["pass"] is False
        assert "single/snippet/all" in v["regressions"]
        assert "single/snippet/prose" in v["regressions"]

    def test_single_flipped_query_is_inside_noise(self):
        base = _run(single=_single_rows({q: 1 for q in IDS}))
        cur_vals = {q: 1 for q in IDS}
        cur_vals[IDS[3]] = 0
        v = evaluate_ratchet(base, _run(single=_single_rows(cur_vals)))
        # One flip in forty: the bootstrap CI reaches zero, so no failure.
        assert v["pass"] is True
        assert v["regressions"] == []

    def test_improvement_is_listed_not_failed(self):
        base = _run(corpus=_corpus_rows({q: 0.0 for q in IDS}))
        cur = _run(corpus=_corpus_rows({q: 1.0 for q in IDS}))
        v = evaluate_ratchet(base, cur)
        assert v["pass"] is True
        assert "corpus/snippet/all" in v["improvements"]

    def test_retrieval_shift_is_a_confound_not_a_failure(self):
        base = _run(corpus=_corpus_rows({q: 1.0 for q in IDS}))
        cur = _run(corpus=_corpus_rows({q: 1.0 for q in IDS}))
        for row in cur["arms"]["corpus"]["rows"]["snippet"].values():
            row["doc_ndcg"] = 0.2
        v = evaluate_ratchet(base, cur)
        assert v["pass"] is True
        assert any("corpus/snippet: doc_ndcg moved" in c for c in v["confounds"])

    def test_query_id_mismatch_is_reported_and_fails(self):
        base = _run(single=_single_rows({q: 1 for q in IDS}))
        cur = _run(single=_single_rows({q: 1 for q in IDS[:-1]}))
        v = evaluate_ratchet(base, cur)
        assert v["pass"] is False
        assert v["id_mismatch"] and v["regressions"] == []

    def test_arm_absent_from_baseline_is_a_mismatch(self):
        base = _run(single=_single_rows({q: 1 for q in IDS}))
        cur = _run(
            corpus=_corpus_rows({q: 1.0 for q in IDS}),
            single=_single_rows({q: 1 for q in IDS}),
        )
        v = evaluate_ratchet(base, cur)
        assert v["id_mismatch"] == ["corpus: absent from baseline"]

    def test_arm_missing_from_current_is_skipped(self):
        base = _run(
            corpus=_corpus_rows({q: 1.0 for q in IDS}),
            single=_single_rows({q: 1 for q in IDS}),
        )
        cur = _run(single=_single_rows({q: 1 for q in IDS}))
        v = evaluate_ratchet(base, cur)
        assert v["pass"] is True
        assert all(c["arm"] == "single" for c in v["cells"])


class TestSummarize:
    def test_per_class_means_and_invariants(self):
        rows = _corpus_rows({"a": 1.0, "b": 0.0}, cls="needle")
        rows.update(_corpus_rows({"c": 0.5}, cls="trap"))
        s = summarize(_run(corpus=rows))
        snip = s["corpus"]["styles"]["snippet"]
        assert snip["classes"]["all"] == {"n": 3, "mean": 0.5}
        assert snip["classes"]["needle"] == {"n": 2, "mean": 0.5}
        assert snip["classes"]["trap"] == {"n": 1, "mean": 0.5}
        assert snip["doc_ndcg"] == 0.8
        assert s["corpus"]["seconds_per_query"]["paragraph"] == 1.5

    def test_single_reports_page_hits(self):
        s = summarize(_run(single=_single_rows({"a": 1, "b": 0})))
        assert s["single"]["styles"]["snippet"]["page_hits"] == 2


class TestRenderMarkdown:
    def test_report_carries_gate_table_and_lists(self):
        base = _run(single=_single_rows({q: 1 for q in IDS}))
        cur = _run(single=_single_rows({q: 0 for q in IDS}))
        v = evaluate_ratchet(base, cur)
        md = render_markdown(cur, summarize(cur), v)
        assert "## Gate: FAIL" in md
        assert "| single | snippet | all | 40 | 1.000 | 0.000 |" in md
        assert "**Regressions:**" in md
        assert "excludes zero" in md

    def test_report_without_baseline_has_no_gate_section(self):
        cur = _run(single=_single_rows({q: 1 for q in IDS}))
        md = render_markdown(cur, summarize(cur), None)
        assert "## single (metric: contains)" in md
        assert "Gate" not in md


class TestMain:
    """`main` takes an injectable runner so the CLI plumbing is testable
    without touching a PDF."""

    @staticmethod
    def _paths(tmp_path: Path) -> list[str]:
        return [
            "--baseline",
            str(tmp_path / "baseline.json"),
            "--output-json",
            str(tmp_path / "results.json"),
            "--output-md",
            str(tmp_path / "results.md"),
        ]

    def test_missing_baseline_is_a_setup_error(self, tmp_path, capsys):
        rc = main(self._paths(tmp_path), runner=lambda *a: _run(single={}))
        assert rc == 2
        assert "no baseline" in capsys.readouterr().err

    def test_update_baseline_creates_it_and_persists_results(self, tmp_path):
        run = _run(single=_single_rows({q: 1 for q in IDS}))
        rc = main(self._paths(tmp_path) + ["--update-baseline"], runner=lambda *a: run)
        assert rc == 0
        assert json.loads((tmp_path / "baseline.json").read_text())["arms"]
        assert (tmp_path / "results.md").read_text().startswith("# Pure-semantic")

    def test_gate_fails_on_regression_and_refuses_to_lower_baseline(self, tmp_path):
        good = _run(single=_single_rows({q: 1 for q in IDS}))
        bad = _run(single=_single_rows({q: 0 for q in IDS}))
        assert (
            main(self._paths(tmp_path) + ["--update-baseline"], runner=lambda *a: good)
            == 0
        )
        rc = main(self._paths(tmp_path), runner=lambda *a: bad)
        assert rc == 1
        rc = main(self._paths(tmp_path) + ["--update-baseline"], runner=lambda *a: bad)
        assert rc == 1
        kept = json.loads((tmp_path / "baseline.json").read_text())
        assert kept["arms"]["single"]["rows"]["snippet"][IDS[0]]["contains"] == 1

    def test_gate_passes_on_identical_run(self, tmp_path):
        run = _run(single=_single_rows({q: 1 for q in IDS}))
        main(self._paths(tmp_path) + ["--update-baseline"], runner=lambda *a: run)
        assert main(self._paths(tmp_path), runner=lambda *a: run) == 0

    def test_id_mismatch_is_a_setup_error(self, tmp_path):
        base = _run(single=_single_rows({q: 1 for q in IDS}))
        cur = _run(single=_single_rows({q: 1 for q in IDS[:10]}))
        main(self._paths(tmp_path) + ["--update-baseline"], runner=lambda *a: base)
        assert main(self._paths(tmp_path), runner=lambda *a: cur) == 2

    def test_calibrate_needs_no_baseline(self, tmp_path):
        run = _run(single=_single_rows({q: 1 for q in IDS}))
        assert main(self._paths(tmp_path) + ["--calibrate"], runner=lambda *a: run) == 0
        assert (tmp_path / "results.json").exists()

    def test_limit_persists_nothing(self, tmp_path):
        run = _run(single=_single_rows({q: 1 for q in IDS}))
        run["config"]["limit"] = 5
        assert (
            main(self._paths(tmp_path) + ["--limit", "5"], runner=lambda *a: run) == 0
        )
        assert not (tmp_path / "results.json").exists()

    def test_runner_setup_failure_is_exit_2(self, tmp_path, capsys):
        def boom(*a):
            raise ValueError("sha256 mismatch")

        rc = main(self._paths(tmp_path) + ["--calibrate"], runner=boom)
        assert rc == 2
        assert "sha256 mismatch" in capsys.readouterr().err

    @pytest.mark.parametrize("arms", ["bogus", ""])
    def test_unknown_arm_is_exit_2(self, tmp_path, arms):
        assert main(self._paths(tmp_path) + ["--arms", arms]) == 2


ENV_313 = {
    "python": "3.13.1",
    "implementation": "CPython",
    "sqlite": "3.51.0",
    "machine": "arm64",
    "platform": "macOS-26.6-arm64",
    "executable": "/main/.venv/bin/python",
    "packages": {"onnxruntime": "1.24.4"},
}
ENV_312 = {**ENV_313, "python": "3.12.12", "sqlite": "3.50.4"}


def _timed(env: dict | None, snippet_spq: float) -> dict:
    run = _run(corpus=_corpus_rows({q: 0.5 for q in IDS}))
    run["arms"]["corpus"]["seconds_per_query"] = {
        "snippet": snippet_spq,
        "paragraph": 1.5,
    }
    if env is not None:
        run["environment"] = env
    return run


class TestTimingAgainstBaseline:
    """s/query is compared with the baseline only when both runs recorded
    the same environment. The 2026-09-12 confound (a worktree venv on
    Python 3.12 / SQLite 3.50 read ~37% slower than the main checkout's
    3.13 / 3.51 on identical code) must print its cause, not a delta."""

    def test_same_environment_reports_the_timing_delta(self):
        v = evaluate_ratchet(_timed(ENV_313, 1.40), _timed(ENV_313, 1.54))
        t = v["timing"]
        assert t["comparable"] is True
        row = next(
            r for r in t["rows"] if r["arm"] == "corpus" and r["style"] == "snippet"
        )
        assert (row["baseline"], row["current"]) == (1.40, 1.54)
        md = render_markdown(_timed(ENV_313, 1.54), summarize(_timed(ENV_313, 1.54)), v)
        assert "## Timing vs baseline" in md
        assert "| corpus | snippet | 1.40 | 1.54 | +10% |" in md

    def test_different_environment_refuses_the_comparison(self):
        cur = _timed(ENV_312, 2.04)
        v = evaluate_ratchet(_timed(ENV_313, 1.47), cur)
        assert v["timing"]["comparable"] is False
        assert "python 3.13.1 != 3.12.12" in v["timing"]["reasons"]
        md = render_markdown(cur, summarize(cur), v)
        assert "Timings are not comparable with the baseline" in md
        assert "python 3.13.1 != 3.12.12" in md
        assert "| corpus | snippet | 1.47 | 2.04" not in md

    def test_baseline_without_environment_is_not_comparable(self):
        v = evaluate_ratchet(_timed(None, 1.47), _timed(ENV_313, 1.50))
        assert v["timing"]["reasons"] == ["baseline recorded no environment"]

    def test_timing_never_fails_the_gate(self):
        v = evaluate_ratchet(_timed(ENV_313, 1.0), _timed(ENV_313, 9.0))
        assert v["pass"] is True
        v = evaluate_ratchet(_timed(ENV_313, 1.0), _timed(ENV_312, 1.0))
        assert v["pass"] is True

    def test_report_names_the_environment_it_ran_in(self):
        run = _timed(ENV_313, 1.5)
        md = render_markdown(run, summarize(run), None)
        assert "Python 3.13.1" in md and "SQLite 3.51.0" in md

    def test_run_all_records_the_environment(self, tmp_path):
        from scripts.benchmark_semantic_excerpts import run_all

        run = run_all((), tmp_path, tmp_path, None)
        assert run["environment"]["python"]
        assert run["environment"]["sqlite"]
