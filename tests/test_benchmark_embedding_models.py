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
