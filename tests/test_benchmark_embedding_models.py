# tests/test_benchmark_embedding_models.py
"""Unit tests for scripts/benchmark_embedding_models.py."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import benchmark_embedding_models as bem  # noqa: E402


class TestLoadGroundTruth:
    def test_loads_existing_corpus(self, tmp_path):
        gt_file = tmp_path / "gt.json"
        gt_file.write_text(
            json.dumps({"pdfs": {"x": {"url": "u", "page_count": 1, "scenarios": {}}}})
        )
        gt = bem.load_ground_truth(str(gt_file))
        assert "pdfs" in gt
        assert "x" in gt["pdfs"]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            bem.load_ground_truth(str(tmp_path / "missing.json"))


class TestStripAnsi:
    def test_strips_color_codes(self):
        assert bem._strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_passthrough_plain_text(self):
        assert bem._strip_ansi("plain") == "plain"


class TestComputeMetrics:
    def test_perfect_recall(self):
        matches = [{"page": 1}, {"page": 2}, {"page": 3}]
        m = bem._compute_metrics(matches, {1, 2}, k=3)
        assert m == {"recall": 1.0, "rr": 1.0, "rank_first_hit": 1}

    def test_partial_recall(self):
        matches = [{"page": 5}, {"page": 1}, {"page": 9}]
        m = bem._compute_metrics(matches, {1, 2}, k=3)
        assert m["recall"] == 0.5
        assert m["rr"] == 0.5  # first hit at rank 2
        assert m["rank_first_hit"] == 2

    def test_no_hits(self):
        m = bem._compute_metrics([{"page": 9}], {1, 2}, k=3)
        assert m == {"recall": 0.0, "rr": 0.0, "rank_first_hit": None}

    def test_empty_relevant(self):
        m = bem._compute_metrics([{"page": 1}], set(), k=3)
        assert m == {"recall": 0.0, "rr": 0.0, "rank_first_hit": None}

    def test_k_truncation(self):
        # Hit at rank 4 should not count when k=3
        matches = [{"page": 9}, {"page": 8}, {"page": 7}, {"page": 1}]
        m = bem._compute_metrics(matches, {1}, k=3)
        assert m == {"recall": 0.0, "rr": 0.0, "rank_first_hit": None}


class TestRunScenario:
    def test_calls_pdf_search_with_semantic_mode(self, monkeypatch):
        captured = {}

        def fake_search(pdf_path, query, mode, max_results):
            captured["mode"] = mode
            captured["max_results"] = max_results
            return {"matches": [{"page": 1}, {"page": 2}]}

        monkeypatch.setattr(bem, "pdf_search", fake_search)
        result = bem._run_scenario("/tmp/x.pdf", "test query", {1}, k=5)
        assert captured["mode"] == "semantic"
        assert captured["max_results"] == 5
        assert result["recall"] == 1.0
        assert result["rr"] == 1.0
        assert result["top_pages"] == [1, 2]

    def test_handles_search_error(self, monkeypatch):
        monkeypatch.setattr(
            bem, "pdf_search", lambda *a, **kw: {"error": "fastembed missing"}
        )
        result = bem._run_scenario("/tmp/x.pdf", "q", {1}, k=5)
        assert result["recall"] == 0.0
        assert result["rr"] == 0.0
        assert result["rank_first_hit"] is None
        assert result["top_pages"] == []


class TestRunLatency:
    def test_returns_median_of_n_runs(self, monkeypatch):
        call_count = {"n": 0}

        def fake_search(*args, **kwargs):
            call_count["n"] += 1
            return {"matches": []}

        monkeypatch.setattr(bem, "pdf_search", fake_search)
        ms = bem.run_latency_probe("/tmp/x.pdf", "q", k=5, n_runs=3)
        assert call_count["n"] == 3
        assert ms >= 0.0

    def test_default_n_runs_is_three(self, monkeypatch):
        call_count = {"n": 0}

        def fake_search(*args, **kwargs):
            call_count["n"] += 1
            return {"matches": []}

        monkeypatch.setattr(bem, "pdf_search", fake_search)
        bem.run_latency_probe("/tmp/x.pdf", "q", k=5)
        assert call_count["n"] == 3


class TestRunModel:
    def test_swaps_config_and_cache_runs_all_scenarios(self, monkeypatch, tmp_path):
        # Minimal ground truth: one PDF, two scenarios
        gt = {
            "pdfs": {
                "fakepaper": {
                    "url": "https://example.com/x.pdf",
                    "title": "X",
                    "page_count": 5,
                    "scenarios": {
                        "1a": {"query": "q1", "relevant_pages": [1]},
                        "1b": {"query": "q2", "relevant_pages": [2]},
                    },
                }
            }
        }
        # Stub _resolve_path to skip URL fetching
        monkeypatch.setattr(bem, "_resolve_path", lambda u: ("/tmp/fake.pdf", None))
        # Stub pdf_search to return the relevant page first
        observed_models = []

        def fake_search(pdf_path, query, mode, max_results):
            # Capture which model is "active" via the patched pdf_config
            observed_models.append(bem.server_module.pdf_config.embedding_model)
            page = 1 if query == "q1" else 2
            return {"matches": [{"page": page}]}

        monkeypatch.setattr(bem, "pdf_search", fake_search)
        # Map scenario id → k value (matches benchmark_rrf.py defaults)
        scenario_k = {"1a": 5, "1b": 5}

        result = bem.run_model(
            model_name="snowflake/snowflake-arctic-embed-s",
            gt=gt,
            scenario_k=scenario_k,
        )

        assert result["model"] == "snowflake/snowflake-arctic-embed-s"
        assert len(result["scenarios"]) == 2
        # Each scenario hit its relevant page → recall 1.0
        assert all(s["recall"] == 1.0 for s in result["scenarios"])
        # Config was swapped during the run
        assert all(m == "snowflake/snowflake-arctic-embed-s" for m in observed_models)
        # Embed-time was measured per PDF
        assert "fakepaper" in result["embed_ms"]
        # Latency probe ran
        assert result["p50_query_ms"] >= 0.0

    def test_restores_config_after_run(self, monkeypatch, tmp_path):
        gt = {
            "pdfs": {
                "x": {
                    "url": "u",
                    "title": "X",
                    "page_count": 1,
                    "scenarios": {
                        "1a": {"query": "q", "relevant_pages": [1]},
                    },
                }
            }
        }
        monkeypatch.setattr(bem, "_resolve_path", lambda u: ("/tmp/x.pdf", None))
        monkeypatch.setattr(
            bem, "pdf_search", lambda *a, **kw: {"matches": [{"page": 1}]}
        )
        original_model = bem.server_module.pdf_config.embedding_model
        bem.run_model(
            model_name="BAAI/bge-base-en-v1.5",
            gt=gt,
            scenario_k={"1a": 5},
        )
        assert bem.server_module.pdf_config.embedding_model == original_model


class TestComputeVerdict:
    def _model_result(self, name, mrr, p50, is_baseline=False):
        return {
            "model": name,
            "mrr": mrr,
            "p50_query_ms": p50,
            "embed_ms": {},
            "scenarios": [],
        }

    def test_no_challenger_passes_keeps_default(self):
        results = [
            self._model_result("BAAI/bge-small-en-v1.5", 0.70, 5.0, True),
            self._model_result("snowflake/snowflake-arctic-embed-s", 0.72, 6.0),
            self._model_result("BAAI/bge-base-en-v1.5", 0.73, 12.0),
        ]
        v = bem.compute_verdict(results, "BAAI/bge-small-en-v1.5")
        assert v["default_changed"] is False
        assert v["winner"] is None
        assert (
            "mrr_lift" in v["reason"].lower() or "no challenger" in v["reason"].lower()
        )

    def test_clear_winner_swaps_default(self):
        results = [
            self._model_result("BAAI/bge-small-en-v1.5", 0.60, 5.0, True),
            self._model_result("snowflake/snowflake-arctic-embed-s", 0.68, 6.0),
        ]
        v = bem.compute_verdict(results, "BAAI/bge-small-en-v1.5")
        assert v["default_changed"] is True
        assert v["winner"] == "snowflake/snowflake-arctic-embed-s"

    def test_latency_blowout_blocks_otherwise_winner(self):
        results = [
            self._model_result("BAAI/bge-small-en-v1.5", 0.60, 5.0, True),
            self._model_result("BAAI/bge-base-en-v1.5", 0.70, 8.0),  # 1.6x latency
        ]
        v = bem.compute_verdict(results, "BAAI/bge-small-en-v1.5")
        assert v["default_changed"] is False
        assert "latency" in v["reason"].lower()

    def test_picks_highest_mrr_when_two_pass(self):
        results = [
            self._model_result("BAAI/bge-small-en-v1.5", 0.60, 5.0, True),
            self._model_result("snowflake/snowflake-arctic-embed-s", 0.66, 6.0),
            self._model_result("BAAI/bge-base-en-v1.5", 0.69, 7.0),
        ]
        v = bem.compute_verdict(results, "BAAI/bge-small-en-v1.5")
        assert v["winner"] == "BAAI/bge-base-en-v1.5"

    def test_baseline_missing_raises(self):
        results = [self._model_result("foo", 0.5, 5.0)]
        with pytest.raises(ValueError):
            bem.compute_verdict(results, "BAAI/bge-small-en-v1.5")


class TestFormatMarkdownTable:
    def test_includes_all_models_and_verdict(self):
        results = [
            {
                "model": "BAAI/bge-small-en-v1.5",
                "mrr": 0.70,
                "p50_query_ms": 5.0,
                "embed_ms": {"a": 1000, "b": 2000},
                "scenarios": [],
            },
            {
                "model": "BAAI/bge-base-en-v1.5",
                "mrr": 0.72,
                "p50_query_ms": 7.0,
                "embed_ms": {"a": 1500, "b": 3000},
                "scenarios": [],
            },
        ]
        verdict = {
            "default_changed": False,
            "winner": None,
            "reason": "No challenger met threshold",
            "baseline": "BAAI/bge-small-en-v1.5",
        }
        out = bem.format_markdown_table(results, verdict)
        assert "BAAI/bge-small-en-v1.5" in out
        assert "BAAI/bge-base-en-v1.5" in out
        assert "0.70" in out
        assert "0.72" in out
        assert "kept" in out.lower() or "no challenger" in out.lower()

    def test_marks_default_changed(self):
        results = [
            {
                "model": "A",
                "mrr": 0.60,
                "p50_query_ms": 5.0,
                "embed_ms": {},
                "scenarios": [],
            },
            {
                "model": "B",
                "mrr": 0.70,
                "p50_query_ms": 6.0,
                "embed_ms": {},
                "scenarios": [],
            },
        ]
        verdict = {
            "default_changed": True,
            "winner": "B",
            "reason": "B wins",
            "baseline": "A",
        }
        out = bem.format_markdown_table(results, verdict)
        assert "changed to `B`" in out


class TestPrintSummary:
    """Exercises the terminal printer; we just check it doesn't crash and
    that the verdict line and per-model rows make it into _OUTPUT.
    """

    def test_prints_verdict_and_models(self):
        bem._OUTPUT.clear()
        results = [
            {
                "model": "BAAI/bge-small-en-v1.5",
                "mrr": 0.70,
                "p50_query_ms": 5.0,
                "embed_ms": {"a": 1000},
                "scenarios": [
                    {
                        "id": "1a",
                        "pdf": "a",
                        "query": "q",
                        "k": 5,
                        "relevant_pages": [1],
                        "recall": 1.0,
                        "rr": 1.0,
                        "rank_first_hit": 1,
                        "top_pages": [1],
                    },
                ],
            },
        ]
        verdict = {
            "default_changed": False,
            "winner": None,
            "reason": "single-model run",
            "baseline": "BAAI/bge-small-en-v1.5",
            "thresholds": {"mrr_lift": 0.05, "latency_ratio": 1.5},
        }
        bem.print_summary(results, verdict)
        out = bem._strip_ansi("\n".join(bem._OUTPUT))
        assert "BAAI/bge-small-en-v1.5" in out
        assert "single-model run" in out


class TestSaveResults:
    def test_writes_txt_and_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bem._OUTPUT.clear()
        bem._OUTPUT.extend(["\x1b[31mhello\x1b[0m", "world"])
        results = [
            {
                "model": "BAAI/bge-small-en-v1.5",
                "mrr": 0.7,
                "p50_query_ms": 5.0,
                "embed_ms": {},
                "scenarios": [],
            }
        ]
        verdict = {
            "default_changed": False,
            "winner": None,
            "reason": "stub",
            "baseline": "BAAI/bge-small-en-v1.5",
            "thresholds": {"mrr_lift": 0.05, "latency_ratio": 1.5},
        }
        bem._save_results(
            results,
            verdict,
            file_timestamp="20260509_120000",
            iso_timestamp="2026-05-09T12:00:00",
        )
        txt = (
            tmp_path / "benchmark_results" / "embedding_models_20260509_120000.txt"
        ).read_text()
        assert "hello\nworld" == txt  # ANSI stripped
        data = json.loads(
            (
                tmp_path / "benchmark_results" / "embedding_models_20260509_120000.json"
            ).read_text()
        )
        assert data["timestamp"] == "2026-05-09T12:00:00"
        assert data["baseline"] == "BAAI/bge-small-en-v1.5"
        assert data["models"][0]["model"] == "BAAI/bge-small-en-v1.5"
        assert data["verdict"]["default_changed"] is False


class TestMainIntegration:
    """Drives main() with a stubbed pdf_search so no real models download."""

    def test_main_runs_end_to_end(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        # Minimal ground truth covering 1 PDF, 2 scenarios
        gt_path = tmp_path / "benchmark_data" / "ground_truth.json"
        gt_path.parent.mkdir(parents=True)
        gt_path.write_text(
            json.dumps(
                {
                    "pdfs": {
                        "fake": {
                            "url": "https://example.com/x.pdf",
                            "title": "X",
                            "page_count": 5,
                            "scenarios": {
                                "1a": {"query": "q1", "relevant_pages": [1]},
                                "1b": {"query": "q2", "relevant_pages": [2]},
                            },
                        }
                    }
                }
            )
        )
        # Stub _resolve_path and pdf_search
        monkeypatch.setattr(bem, "_resolve_path", lambda u: ("/tmp/fake.pdf", None))
        monkeypatch.setattr(
            bem,
            "pdf_search",
            lambda pdf, q, mode, max_results: {
                "matches": [{"page": 1 if q == "q1" else 2}]
            },
        )
        # Run main() with the test ground truth
        monkeypatch.setattr(
            sys,
            "argv",
            ["benchmark_embedding_models.py", "--ground-truth", str(gt_path)],
        )
        bem._OUTPUT.clear()
        bem.main()

        # Output files exist and the verdict line appeared on stdout
        results_dir = tmp_path / "benchmark_results"
        assert results_dir.exists()
        json_files = list(results_dir.glob("embedding_models_*.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text())
        # All configured models ran (against the stubbed pdf_search)
        assert len(data["models"]) == len(bem.MODELS)
        assert data["verdict"]["baseline"] == "BAAI/bge-small-en-v1.5"
        out = bem._strip_ansi("\n".join(bem._OUTPUT))
        assert "Verdict" in out
