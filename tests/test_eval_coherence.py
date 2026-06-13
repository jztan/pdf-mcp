"""Tests for the extraction-coherence eval harness (pure logic; fake judge)."""

from __future__ import annotations

import sys
from pathlib import Path

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
