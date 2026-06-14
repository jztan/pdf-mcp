"""Tests for the extraction-coherence eval harness (pure logic; fake judge)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import eval_coherence as ec  # noqa: E402


def test_parse_verdict_well_formed():
    raw = '{"verdict": "coherent", "rationale": "reads fine", "confidence": "high"}'
    v = ec.parse_verdict(raw)
    assert v.verdict == "coherent"
    assert v.rationale == "reads fine"


def test_parse_verdict_unknown_label_is_error():
    raw = '{"verdict": "great", "rationale": "x", "confidence": "low"}'
    assert ec.parse_verdict(raw).verdict == "error"


def test_parse_verdict_malformed_json_is_error():
    assert ec.parse_verdict("not json").verdict == "error"


def test_majority_clear_winner():
    votes = [ec.Verdict("coherent"), ec.Verdict("coherent"), ec.Verdict("partial")]
    assert ec.majority_verdict(votes).verdict == "coherent"


def test_majority_no_winner_is_error():
    votes = [ec.Verdict("coherent"), ec.Verdict("partial"), ec.Verdict("scrambled")]
    assert ec.majority_verdict(votes).verdict == "error"


def test_majority_errors_dominate_to_error():
    votes = [ec.Verdict("error"), ec.Verdict("error"), ec.Verdict("coherent")]
    assert ec.majority_verdict(votes).verdict == "error"


def test_compare_regression_detected():
    diffs = ec.compare({"p": "coherent"}, {"p": "scrambled"})
    assert diffs["p"] == "regressed"


def test_compare_improvement_detected():
    assert ec.compare({"p": "scrambled"}, {"p": "coherent"})["p"] == "improved"


def test_compare_equal_is_same():
    assert ec.compare({"p": "partial"}, {"p": "partial"})["p"] == "same"


def test_compare_error_current_is_error():
    assert ec.compare({"p": "coherent"}, {"p": "error"})["p"] == "error"


def test_compare_unavailable_excluded():
    assert ec.compare({"p": "coherent"}, {"p": "unavailable"})["p"] == "unavailable"


def test_has_regression_true_only_on_regress_or_error():
    assert ec.has_regression({"p": "regressed"}) is True
    assert ec.has_regression({"p": "error"}) is True
    assert ec.has_regression({"p": "improved", "q": "same"}) is False


def test_config_matches_identical():
    cfg = {"column_aware": True, "vertical_aware": False, "semantic": True}
    assert ec.config_matches(cfg, dict(cfg)) is True


def test_config_matches_difference():
    a = {"column_aware": True, "vertical_aware": False}
    b = {"column_aware": True, "vertical_aware": True}
    assert ec.config_matches(a, b) is False


# ---------------------------------------------------------------------------
# T6: judge wiring + calibration (fake judge — no real CLI calls)
# ---------------------------------------------------------------------------


def _fake_judge(mapping):
    """Return a judge(text, direction) that looks up a verdict by exact text."""

    def judge(text, direction):
        return ec.Verdict(mapping.get(text, "scrambled"))

    return judge


def test_calibrate_passes_when_judge_correct():
    fixtures = [
        {"id": "a", "text": "good", "direction": "ltr", "expected": "coherent"},
        {"id": "b", "text": "bad", "direction": "ltr", "expected": "scrambled"},
    ]
    judge = _fake_judge({"good": "coherent", "bad": "scrambled"})
    ok, failures = ec.calibrate(fixtures, judge)
    assert ok is True
    assert failures == []


def test_calibrate_fails_when_judge_wrong():
    fixtures = [{"id": "a", "text": "good", "direction": "ltr", "expected": "coherent"}]
    judge = _fake_judge({"good": "scrambled"})
    ok, failures = ec.calibrate(fixtures, judge)
    assert ok is False
    assert failures and failures[0]["id"] == "a"


# ---------------------------------------------------------------------------
# T7: resolve_pdf + extract_page_text (no network, monkeypatch)
# ---------------------------------------------------------------------------


def test_resolve_pdf_missing_local_returns_none():
    entry = {"id": "x", "source": {"local": "/no/such/file.pdf"}, "page": 1}
    assert ec.resolve_pdf(entry) is None  # -> 'unavailable' upstream


def test_extract_page_text_unavailable_when_pdf_none(monkeypatch):
    monkeypatch.setattr(ec, "resolve_pdf", lambda entry: None)
    text, status = ec.extract_page_text({"id": "x", "source": {}, "page": 1})
    assert text is None and status == "unavailable"


# ---------------------------------------------------------------------------
# T8: format_report
# ---------------------------------------------------------------------------


def test_format_report_lists_known_bad_section():
    verdicts = {
        "a": ec.Verdict("coherent", "ok"),
        "b": ec.Verdict("scrambled", "soup"),
    }
    diff = {"a": "same", "b": "same"}
    report = ec.format_report(verdicts, diff, {"vertical_aware": True})
    assert "scrambled" in report
    # known-bad section names the still-bad page
    assert "Known-bad" in report and "b" in report
    # green-ish diff must NOT hide that b is still scrambled
    assert "a" in report


# ---------------------------------------------------------------------------
# T9: baseline round-trip
# ---------------------------------------------------------------------------


def test_baseline_roundtrip(tmp_path):
    path = tmp_path / "baseline.json"
    cfg = {"vertical_aware": True}
    ec.write_baseline(path, {"a": ec.Verdict("coherent")}, cfg, model="m")
    verdicts, loaded_cfg = ec.read_baseline(path)
    assert verdicts == {"a": "coherent"}
    assert loaded_cfg == cfg


def test_read_baseline_missing_returns_empty(tmp_path):
    verdicts, cfg = ec.read_baseline(tmp_path / "nope.json")
    assert verdicts == {} and cfg == {}


# ---------------------------------------------------------------------------
# Live guard: full corpus run vs the committed baseline (real claude CLI).
# Slow + billed; skipped when the authenticated `claude` CLI is unavailable.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="coherence guard needs the authenticated claude CLI + network",
)
def test_coherence_no_regression_vs_baseline():
    """Full run over the corpus; assert no page regressed vs the baseline."""
    data = Path(__file__).parent.parent / "benchmark_data"
    calib = ec.json.loads((data / "coherence_calibration.json").read_text("utf-8"))
    corpus = ec.json.loads((data / "coherence_corpus.json").read_text("utf-8"))
    judge = ec.make_claude_judge()
    ok, failures = ec.calibrate(calib["fixtures"], judge)
    assert ok, f"judge calibration failed — eval invalid: {failures}"
    base_verdicts, _ = ec.read_baseline(data / "coherence_baseline.json")
    current = {}
    for entry in corpus["pages"]:
        text, status = ec.extract_page_text(entry)
        if status != "ok":
            current[entry["id"]] = "unavailable"
            continue
        current[entry["id"]] = ec.judge_majority(
            text, entry["direction"], judge
        ).verdict
    diff = ec.compare(base_verdicts, current)
    regressions = {k: v for k, v in diff.items() if v in ("regressed", "error")}
    assert not regressions, f"coherence regressions vs baseline: {regressions}"
