"""Pure-logic tests for the Bedrock KB anchor benchmark harness."""

from pathlib import Path

from scripts.benchmark_bedrock_kb import check_corpus_quota


class TestCheckCorpusQuota:
    def test_passes_when_every_file_is_under_limit(self, tmp_path: Path):
        (tmp_path / "a.pdf").write_bytes(b"x" * 10)
        manifest = {"docs": [{"id": "a", "path": "a.pdf"}]}
        assert check_corpus_quota(manifest, tmp_path, limit_bytes=100) == []

    def test_reports_each_file_over_limit_with_size(self, tmp_path: Path):
        (tmp_path / "big.pdf").write_bytes(b"x" * 200)
        (tmp_path / "ok.pdf").write_bytes(b"x" * 10)
        manifest = {
            "docs": [
                {"id": "big", "path": "big.pdf"},
                {"id": "ok", "path": "ok.pdf"},
            ]
        }
        errors = check_corpus_quota(manifest, tmp_path, limit_bytes=100)
        assert len(errors) == 1
        assert "big" in errors[0] and "200" in errors[0]

    def test_reports_missing_file(self, tmp_path: Path):
        manifest = {"docs": [{"id": "gone", "path": "gone.pdf"}]}
        errors = check_corpus_quota(manifest, tmp_path, limit_bytes=100)
        assert errors == ["gone: missing at gone.pdf"]


from scripts.benchmark_bedrock_kb import cap_to_budget, estimate_tokens  # noqa: E402


class TestEstimateTokens:
    def test_four_chars_per_token_floor(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens("abc") == 0
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("a" * 8000) == 2000


class TestCapToBudget:
    def _u(self, i: int, chars: int):
        return (f"d{i}", i, "x" * chars)

    def test_keeps_units_in_order_until_budget(self):
        units = [self._u(1, 4000), self._u(2, 4000), self._u(3, 4000)]
        kept, k = cap_to_budget(units, budget_tokens=2000)
        assert kept == units[:2]
        assert k == 2

    def test_first_unit_always_kept_even_if_oversized(self):
        units = [self._u(1, 40000), self._u(2, 40)]
        kept, k = cap_to_budget(units, budget_tokens=2000)
        assert kept == units[:1]
        assert k == 1

    def test_stops_at_first_unit_that_does_not_fit(self):
        # unit 2 does not fit, unit 3 would, but order is rank order
        units = [self._u(1, 4000), self._u(2, 8000), self._u(3, 40)]
        kept, k = cap_to_budget(units, budget_tokens=2000)
        assert kept == units[:1]
        assert k == 1

    def test_empty(self):
        assert cap_to_budget([], 2000) == ([], 0)


from scripts.benchmark_bedrock_kb import contain, grade_containment  # noqa: E402


class TestContain:
    def test_exact_substring(self):
        assert contain("the Noetherian type of X", "Noetherian type") == "exact"

    def test_normalized_only_when_whitespace_or_case_differs(self):
        ctx = "SPLITTING\n  FAMILIES and\tthe noetherian TYPE"
        assert contain(ctx, "Splitting families and the Noetherian type") == (
            "normalized"
        )

    def test_missing(self):
        assert contain("unrelated text", "Noetherian type") == "missing"

    def test_exact_wins_over_normalized(self):
        assert contain("a b", "a b") == "exact"


class TestGradeContainment:
    def _q(self, *evidence: str):
        return {
            "id": "q",
            "class": "spread",
            "labels": [
                {"doc": f"d{i}", "page": 1, "gain": 2, "evidence": e}
                for i, e in enumerate(evidence)
            ],
        }

    def test_exact_hit_is_recall_one_no_gap(self):
        kept = [("d0", 1, "... Noetherian type ...")]
        g = grade_containment(self._q("Noetherian type"), kept)
        assert g == {"span_recall": 1.0, "fidelity_gap": 0.0, "status": "exact"}

    def test_normalized_hit_is_recall_one_with_gap(self):
        kept = [("d0", 1, "... noetherian\n type ...")]
        g = grade_containment(self._q("Noetherian type"), kept)
        assert g == {"span_recall": 1.0, "fidelity_gap": 1.0, "status": "normalized"}

    def test_any_label_suffices_for_spread(self):
        kept = [("d1", 1, "second span here")]
        g = grade_containment(self._q("first span", "second span"), kept)
        assert g["span_recall"] == 1.0

    def test_missing(self):
        g = grade_containment(self._q("nope"), [("d0", 1, "x")])
        assert g == {"span_recall": 0.0, "fidelity_gap": 0.0, "status": "missing"}

    def test_labels_without_page_are_ignored(self):
        q = {"id": "q", "class": "route", "labels": [{"doc": "d", "gain": 2}]}
        g = grade_containment(q, [("d", 1, "anything")])
        assert g["status"] == "missing"

    def test_span_split_across_two_kept_units_does_not_match(self):
        kept = [("d0", 1, "Noether"), ("d0", 2, "ian type")]
        # split across units must NOT match; containment is per unit, never
        # across a concatenation of the kept units
        g = grade_containment(self._q("Noetherian type"), kept)
        assert g["span_recall"] == 0.0


from scripts.benchmark_bedrock_kb import bootstrap_diff_ci, no_arm_found  # noqa: E402


class TestBootstrapDiffCi:
    def test_identical_arms_give_zero_diff_and_ci_includes_zero(self):
        a = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0]
        r = bootstrap_diff_ci(a, list(a), n_boot=500, seed=1)
        assert r["mean_diff"] == 0.0
        assert r["lo"] <= 0.0 <= r["hi"]
        assert r["includes_zero"] is True
        assert r["n"] == 8

    def test_clear_gap_excludes_zero(self):
        a = [1.0] * 20
        b = [0.0] * 20
        r = bootstrap_diff_ci(a, b, n_boot=500, seed=1)
        assert r["mean_diff"] == 1.0
        assert r["includes_zero"] is False

    def test_is_deterministic_for_a_seed(self):
        a = [1.0, 0.0, 1.0, 0.0, 1.0]
        b = [0.0, 0.0, 1.0, 0.0, 1.0]
        assert bootstrap_diff_ci(a, b, seed=7) == bootstrap_diff_ci(a, b, seed=7)

    def test_rejects_unpaired_lengths(self):
        import pytest

        with pytest.raises(ValueError):
            bootstrap_diff_ci([1.0], [1.0, 0.0])

    def test_paired_resampling_gives_zero_width_ci_when_diffs_are_degenerate(self):
        # a and b are identical high-variance lists, so every paired
        # difference is exactly 0: a correct paired bootstrap resamples
        # query indices and can only ever produce a diff of 0, giving a
        # zero-width CI (lo == hi == 0.0). If bootstrap_diff_ci were
        # changed to resample a and b independently instead of resampling
        # the paired differences, each resample would draw its own mix of
        # 1.0s and 0.0s from the two high-variance lists and the CI would
        # come back with real width. The other tests in this class cannot
        # catch that regression: their fixtures are either already
        # identical (so independent resampling still lands near 0 width)
        # or have low enough per-query variance that the difference is
        # not obvious.
        a = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]
        b = list(a)
        r = bootstrap_diff_ci(a, b, n_boot=500, seed=3)
        assert r["lo"] == 0.0 and r["hi"] == 0.0


class TestNoArmFound:
    def test_only_queries_missing_everywhere_are_flagged(self):
        status = {
            "P": {"q1": "missing", "q2": "exact", "q3": "missing"},
            "B0": {"q1": "missing", "q2": "missing", "q3": "normalized"},
        }
        assert no_arm_found(status) == ["q1"]

    def test_empty(self):
        assert no_arm_found({}) == []


from scripts.benchmark_bedrock_kb import matches_to_units  # noqa: E402


class TestMatchesToUnits:
    def test_maps_path_to_doc_id_and_keeps_rank_order(self):
        matches = [
            {"path": "/abs/a.pdf", "page": 3, "excerpt": "AAA"},
            {"path": "/abs/b.pdf", "page": 1, "excerpt": "BBB"},
        ]
        units = matches_to_units(matches, {"/abs/a.pdf": "a", "/abs/b.pdf": "b"})
        assert units == [("a", 3, "AAA"), ("b", 1, "BBB")]

    def test_unknown_path_keeps_path_as_id(self):
        units = matches_to_units([{"path": "/x.pdf", "page": 1, "excerpt": ""}], {})
        assert units == [("/x.pdf", 1, "")]


from scripts.benchmark_bedrock_kb import (  # noqa: E402
    render_markdown,
    summarize,
    write_results,
)


def _row(cls, status, k=3, doc_ndcg=1.0, dochit3=1):
    return {
        "class": cls,
        "kept": [],
        "realized_k": k,
        "containment": {
            "span_recall": 0.0 if status == "missing" else 1.0,
            "fidelity_gap": 1.0 if status == "normalized" else 0.0,
            "status": status,
        },
        "doc_ndcg": doc_ndcg,
        "dochit3": dochit3,
        "seconds": 0.1,
    }


class TestSummarize:
    def test_per_class_means_and_paired_diffs(self):
        rows = {
            "P": {"q1": _row("needle", "exact"), "q2": _row("needle", "missing")},
            "B0": {"q1": _row("needle", "normalized"), "q2": _row("needle", "missing")},
        }
        s = summarize(rows, ["needle"], anchor_arms=("B0",), ref_arm="P")
        # q2 is missing in every arm, so no_arm_found flags it and it is
        # excluded from every mean (per the flagged-exclusion contract
        # exercised below in test_flagged_queries_are_excluded_from_means).
        # Only q1 remains: P is exact (span_recall 1.0), B0 is normalized
        # (fidelity_gap 1.0).
        assert s["per_class"]["needle"]["P"]["span_recall"] == 1.0
        assert s["per_class"]["needle"]["B0"]["fidelity_gap"] == 1.0
        assert s["per_class"]["needle"]["P"]["mean_k"] == 3.0
        assert s["diffs"]["needle"]["B0"]["mean_diff"] == 0.0
        assert s["flagged"] == ["q2"]

    def test_flagged_queries_are_excluded_from_means(self):
        rows = {
            "P": {"q1": _row("trap", "exact"), "q2": _row("trap", "missing")},
            "B0": {"q1": _row("trap", "exact"), "q2": _row("trap", "missing")},
        }
        s = summarize(rows, ["trap"], anchor_arms=("B0",))
        assert s["per_class"]["trap"]["P"]["n"] == 1
        assert s["per_class"]["trap"]["P"]["span_recall"] == 1.0


class TestRenderAndWrite:
    def test_markdown_has_one_table_per_class_and_no_aggregate(self):
        rows = {
            "P": {"q1": _row("needle", "exact")},
            "B0": {"q1": _row("needle", "exact")},
        }
        s = summarize(rows, ["needle"], anchor_arms=("B0",))
        md = render_markdown(s, {"budget_tokens": 2000})
        assert "## needle" in md
        assert "realized k" in md
        assert "overall" not in md.lower()

    def test_write_results_creates_both_files(self, tmp_path: Path):
        rows = {
            "P": {"q1": _row("needle", "exact")},
            "B0": {"q1": _row("needle", "exact")},
        }
        s = summarize(rows, ["needle"], anchor_arms=("B0",))
        write_results(s, rows, {"budget_tokens": 2000}, tmp_path)
        assert (tmp_path / "results.json").exists()
        assert (tmp_path / "RESULTS.md").exists()


from scripts.benchmark_bedrock_kb import bedrock_results_to_units  # noqa: E402


class TestBedrockResultsToUnits:
    def _r(self, key, text, page=None):
        r = {
            "content": {"text": text},
            "location": {"type": "S3", "s3Location": {"uri": f"s3://b/{key}"}},
            "metadata": {},
        }
        if page is not None:
            r["metadata"]["x-amz-bedrock-kb-document-page-number"] = page
        return r

    def test_maps_uri_stem_and_float_page(self):
        res = [self._r("0705.4297.pdf", "AAA", 3.0), self._r("x.pdf", "BBB")]
        units = bedrock_results_to_units(res, {"0705.4297": "0705.4297"})
        assert units == [("0705.4297", 3, "AAA"), ("x", None, "BBB")]


import math  # noqa: E402

import pytest  # noqa: E402

from scripts.benchmark_bedrock_kb import run_arm_bedrock, run_arm_p  # noqa: E402


class _FakeRuntime:
    """Stand-in for a bedrock-agent-runtime client. No AWS calls.

    retrieve() returns a fixed retrievalResults list; rerank() returns a
    fixed reordering (by result index) regardless of the actual sources
    passed in, which is all these tests need.
    """

    def __init__(self, retrieval_results, rerank_order=None):
        self._retrieval_results = retrieval_results
        self._rerank_order = rerank_order or []

    def retrieve(self, **kwargs):
        return {"retrievalResults": self._retrieval_results}

    def rerank(self, **kwargs):
        return {
            "results": [{"index": i, "relevanceScore": 1.0} for i in self._rerank_order]
        }


def _result(stem, page, text):
    return {
        "content": {"text": text},
        "location": {"type": "S3", "s3Location": {"uri": f"s3://b/{stem}.pdf"}},
        "metadata": {"x-amz-bedrock-kb-document-page-number": page},
    }


class TestRunArmBedrockDedupWindow:
    def test_doc_ndcg_dedupes_over_the_full_window_not_a_10_item_slice(self):
        # First 10 raw chunks are all the SAME (non-gold) document; the gold
        # document appears only at raw position 11. A correct dedup-before-
        # trim (mirroring run_arm_p) still finds the gold doc at rank 2 of
        # the deduped list. A buggy pre-slice to units[:10] would never see
        # it and score doc_ndcg 0.0.
        results = [_result("A", i + 1, "x" * 4) for i in range(10)]
        results.append(_result("B", 5, "y" * 4))
        runtime = _FakeRuntime(results)
        query = {
            "id": "q1",
            "class": "needle",
            "query": "test query",
            "labels": [{"doc": "B", "page": 5, "gain": 2}],
        }
        rows = run_arm_bedrock(
            runtime,
            "KB1234567890",
            [query],
            {"A": "A", "B": "B"},
            budget_tokens=100_000,
            rerank_model=None,
        )
        # doc_ranked_gains after full-window dedup: [A -> 0.0, B -> 2.0].
        # idcg = 2.0 / log2(2) = 2.0; dcg = 0/log2(2) + 2/log2(3).
        expected = (2.0 / math.log2(3)) / 2.0
        assert rows["q1"]["doc_ndcg"] == pytest.approx(expected)
        assert rows["q1"]["doc_ndcg"] > 0.0


class TestRunArmBedrockRerankOrdering:
    def test_rerank_reorders_before_cap_to_budget(self):
        # Two units, retrieved in order [X, Y]. The stub rerank puts Y
        # first. Each unit alone fills the whole budget (cap_to_budget
        # always keeps the first unit and stops before the second), so
        # which document is kept proves whether reordering happened before
        # capping.
        results = [_result("X", 1, "a" * 4000), _result("Y", 1, "b" * 4000)]
        runtime = _FakeRuntime(results, rerank_order=[1, 0])
        query = {"id": "q1", "class": "needle", "query": "q", "labels": []}
        rows = run_arm_bedrock(
            runtime,
            "KB1234567890",
            [query],
            {"X": "X", "Y": "Y"},
            budget_tokens=1000,
            rerank_model="cohere.rerank-v3-5:0",
        )
        assert rows["q1"]["kept"] == [("Y", 1)]
        assert rows["q1"]["realized_k"] == 1


class TestRunArmBedrockRowShapeParity:
    def test_row_keys_match_real_run_arm_p_output(self, monkeypatch):
        # Compare against a REAL run_arm_p row, not the hand-written _row()
        # fixture (that fixture is only for TestSummarize; using it here
        # would let the two arms' row shapes drift apart silently if
        # run_arm_p ever changed without _row() being updated in lockstep).
        # summarize() consumes both arms interchangeably, so this needs to
        # catch a divergence in either direction.
        #
        # Key-set equality alone is not enough (that is the parity
        # antipattern this repo was burned by once already, see
        # corpus source/FTS5, 2026-07-25): also assert value shapes, so a
        # change to either arm's `kept`/`containment` structure that keeps
        # the same key names but changes what they hold is still caught.
        query = {
            "id": "q1",
            "class": "needle",
            "query": "hello",
            "labels": [{"doc": "A", "page": 1, "gain": 2, "evidence": "hello"}],
        }

        import pdf_mcp.server as pdf_mcp_server

        def fake_pdf_corpus_search(
            paths, q, mode="auto", top_k=25, excerpt_style="paragraph"
        ):
            return {
                "matches": [
                    {"path": "/abs/a.pdf", "page": 1, "excerpt": "hello world"}
                ],
                "coverage": {"searched": len(paths)},
            }

        monkeypatch.setattr(pdf_mcp_server, "pdf_corpus_search", fake_pdf_corpus_search)
        row_p = run_arm_p(
            ["/abs/a.pdf"],
            [query],
            {"/abs/a.pdf": "A"},
            budget_tokens=100_000,
        )["q1"]

        runtime = _FakeRuntime([_result("A", 1, "hello world")])
        row_b = run_arm_bedrock(
            runtime,
            "KB1234567890",
            [query],
            {"A": "A"},
            budget_tokens=100_000,
            rerank_model=None,
        )["q1"]

        assert set(row_p.keys()) == set(row_b.keys())

        for row in (row_p, row_b):
            assert isinstance(row["kept"], list)
            for unit in row["kept"]:
                assert isinstance(unit, tuple)
                assert len(unit) == 2

        assert set(row_p["containment"].keys()) == {
            "span_recall",
            "fidelity_gap",
            "status",
        }
        assert set(row_p["containment"].keys()) == set(row_b["containment"].keys())
        for key in ("span_recall", "fidelity_gap", "status"):
            assert type(row_p["containment"][key]) is type(row_b["containment"][key])


import json  # noqa: E402
import sys  # noqa: E402

import scripts.benchmark_bedrock_kb as bm  # noqa: E402

boto3 = pytest.importorskip("boto3")


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")


class TestMainDriftGuard:
    """Exercises main()'s drift guard and its canonical `.stack.json` path,
    driving `main(argv=[...])` the way tests/test_benchmark_sections.py
    drives its own main(). No AWS calls: stack_outputs, load_state, and
    ingest_stamp_matches are monkeypatched on the bare `_bedrock_kb` module
    object -- the same one main()'s own local `from _bedrock_kb import
    ...` resolves to at call time, since both reach it via the
    scripts/-on-sys.path hack under the bare name `_bedrock_kb` (as
    opposed to the `scripts._bedrock_kb` identity used elsewhere in this
    suite, which is a distinct module object for the same file and would
    not be seen by main())."""

    def _setup(self, tmp_path, monkeypatch):
        sys.path.insert(0, str(bm.REPO / "scripts"))
        import _bedrock_kb

        out_dir = tmp_path / "canonical_out"
        out_dir.mkdir()
        arm_cfg = {"label": "B0", "rerank": None}
        config = {
            "region": "us-east-1",
            "arms": {"P": {"tool": "pdf_corpus_search"}, "B0-default-v1": arm_cfg},
        }
        _write_json(out_dir / "config.json", config)
        monkeypatch.setattr(bm, "OUT_DIR", out_dir)

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _write_json(data_dir / "manifest.json", {"docs": []})
        _write_json(data_dir / "queries.json", {"queries": []})

        return _bedrock_kb, arm_cfg, data_dir, out_dir

    def _fake_stack_outputs(self, tag_value):
        return lambda cfn, name: {
            "tags": {"pdfmcp:arm_config_sha256": tag_value},
            "KnowledgeBaseId": "kb",
            "DataSourceId": "ds",
        }

    def test_returns_2_on_arm_config_tag_mismatch(self, tmp_path, monkeypatch):
        bkb, arm_cfg, data_dir, out_dir = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(
            bkb, "stack_outputs", self._fake_stack_outputs("stale-hash")
        )
        monkeypatch.setattr(
            bkb,
            "load_state",
            lambda path: {"B0-default-v1": {"ingested": True, "stamp": {}}},
        )
        monkeypatch.setattr(bkb, "ingest_stamp_matches", lambda stamp, path: [])

        rc = bm.main(
            [
                "--arms",
                "B0-default-v1",
                "--data-dir",
                str(data_dir),
                "--out-dir",
                str(tmp_path / "pilot"),
            ]
        )
        assert rc == 2

    def test_returns_2_on_manifest_stamp_mismatch(self, tmp_path, monkeypatch):
        bkb, arm_cfg, data_dir, out_dir = self._setup(tmp_path, monkeypatch)
        current = bkb.sha256_json(arm_cfg)
        monkeypatch.setattr(bkb, "stack_outputs", self._fake_stack_outputs(current))
        monkeypatch.setattr(
            bkb,
            "load_state",
            lambda path: {"B0-default-v1": {"ingested": True, "stamp": {}}},
        )
        monkeypatch.setattr(
            bkb, "ingest_stamp_matches", lambda stamp, path: ["manifest"]
        )

        rc = bm.main(
            [
                "--arms",
                "B0-default-v1",
                "--data-dir",
                str(data_dir),
                "--out-dir",
                str(tmp_path / "pilot"),
            ]
        )
        assert rc == 2

    def test_stack_json_read_from_canonical_out_dir_not_pilot_out_dir(
        self, tmp_path, monkeypatch
    ):
        bkb, arm_cfg, data_dir, out_dir = self._setup(tmp_path, monkeypatch)
        current = bkb.sha256_json(arm_cfg)
        monkeypatch.setattr(bkb, "stack_outputs", self._fake_stack_outputs(current))
        seen_paths = []

        def fake_load_state(path):
            seen_paths.append(path)
            return {"B0-default-v1": {"ingested": True, "stamp": {}}}

        monkeypatch.setattr(bkb, "load_state", fake_load_state)
        monkeypatch.setattr(bkb, "ingest_stamp_matches", lambda stamp, path: [])

        pilot_dir = tmp_path / "pilot"
        rc = bm.main(
            [
                "--arms",
                "B0-default-v1",
                "--data-dir",
                str(data_dir),
                "--out-dir",
                str(pilot_dir),
            ]
        )
        assert rc == 0
        assert seen_paths == [out_dir / ".stack.json"]
        assert (pilot_dir / "results.json").exists()
        assert not (out_dir / "results.json").exists()


class TestReuseBedrockRows:
    """`--reuse-bedrock-from PATH` loads stored Bedrock rows instead of
    querying AWS. The happy path must work with boto3 AND botocore made
    unimportable, proving the offline claim rather than asserting it."""

    def _row(self, cls, status="exact"):
        return {
            "class": cls,
            "kept": [],
            "realized_k": 3,
            "containment": {
                "span_recall": 0.0 if status == "missing" else 1.0,
                "fidelity_gap": 0.0,
                "status": status,
            },
            "doc_ndcg": 1.0,
            "dochit3": 1,
            "seconds": 0.1,
        }

    def _setup(
        self,
        tmp_path,
        monkeypatch,
        *,
        budget=2000,
        prior_qids=None,
        arm_hash=None,
        manifest_hash=None,
    ):
        import hashlib

        out_dir = tmp_path / "canonical_out"
        out_dir.mkdir()
        arm_cfg = {"label": "B0", "rerank": None}
        config = {
            "region": "us-east-1",
            "arms": {"P": {"tool": "pdf_corpus_search"}, "B0-default-v1": arm_cfg},
        }
        _write_json(out_dir / "config.json", config)
        monkeypatch.setattr(bm, "OUT_DIR", out_dir)

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _write_json(data_dir / "manifest.json", {"docs": []})
        qids = ["q1", "q2"]
        _write_json(
            data_dir / "queries.json",
            {"queries": [{"id": q, "class": "needle", "labels": []} for q in qids]},
        )
        # arm P stubbed: no corpus, no cache
        monkeypatch.setattr(
            bm, "run_arm_p", lambda *a, **k: {q: self._row("needle") for q in qids}
        )
        real_arm_hash = bm._sha256_json(arm_cfg)
        real_manifest_hash = hashlib.sha256(
            (data_dir / "manifest.json").read_bytes()
        ).hexdigest()
        prior = {
            "config": {
                "budget_tokens": budget,
                "arm_ids": {"B0": "B0-default-v1"},
                "index_stamps": {
                    "B0": {
                        "arm_config_sha256": arm_hash or real_arm_hash,
                        "manifest_sha256": manifest_hash or real_manifest_hash,
                    }
                },
            },
            "per_query": {
                "B0": {q: self._row("needle", "missing") for q in (prior_qids or qids)}
            },
        }
        prior_path = tmp_path / "prior.json"
        _write_json(prior_path, prior)
        return data_dir, out_dir, prior_path

    def _argv(self, data_dir, out_dir, prior_path, budget=2000):
        return [
            "--arms",
            "P,B0-default-v1",
            "--data-dir",
            str(data_dir),
            "--out-dir",
            str(out_dir / "run"),
            "--budget",
            str(budget),
            "--reuse-bedrock-from",
            str(prior_path),
        ]

    def test_happy_path_is_fully_offline(self, tmp_path, monkeypatch):
        data_dir, out_dir, prior = self._setup(tmp_path, monkeypatch)
        # make any `import boto3` / `import botocore` raise ImportError
        monkeypatch.setitem(sys.modules, "boto3", None)
        monkeypatch.setitem(sys.modules, "botocore", None)

        rc = bm.main(self._argv(data_dir, out_dir, prior))
        assert rc == 0
        res = json.loads((out_dir / "run" / "results.json").read_text())
        assert "B0" in res["per_query"] and "P" in res["per_query"]
        assert "B0" in res["summary"]["diffs"]["needle"]
        # P exact vs B0 missing on both queries -> P minus B0 = +1.0
        assert res["summary"]["diffs"]["needle"]["B0"]["mean_diff"] == 1.0
        assert res["config"]["bedrock_live_check"] is False
        assert res["config"]["bedrock_rows_reused_from"] == str(prior)
        assert "arm_config_sha256" in res["config"]["index_stamps"]["B0"]

    def test_refuses_arm_config_drift(self, tmp_path, monkeypatch):
        d, o, p = self._setup(tmp_path, monkeypatch, arm_hash="stale")
        assert bm.main(self._argv(d, o, p)) == 2

    def test_refuses_manifest_drift(self, tmp_path, monkeypatch):
        d, o, p = self._setup(tmp_path, monkeypatch, manifest_hash="stale")
        assert bm.main(self._argv(d, o, p)) == 2

    def test_refuses_query_set_mismatch(self, tmp_path, monkeypatch):
        d, o, p = self._setup(tmp_path, monkeypatch, prior_qids=["q1"])
        assert bm.main(self._argv(d, o, p)) == 2

    def test_refuses_budget_mismatch(self, tmp_path, monkeypatch):
        d, o, p = self._setup(tmp_path, monkeypatch, budget=1000)
        assert bm.main(self._argv(d, o, p, budget=2000)) == 2


def test_local_sha256_json_matches_bedrock_kb_helper():
    """The offline reuse path hashes locally to avoid importing _bedrock_kb
    (botocore at module top). A divergence would make every stored stamp
    look drifted, so the two implementations are pinned together here."""
    sys.path.insert(0, str(bm.REPO / "scripts"))
    import _bedrock_kb

    for obj in ({"a": 1, "b": [2, 3]}, {"label": "B0", "rerank": None}, {"z": "é"}):
        assert bm._sha256_json(obj) == _bedrock_kb.sha256_json(obj)
