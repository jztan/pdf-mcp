"""Unit tests for scripts/benchmark_mlx_backend.py result saving."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import benchmark_mlx_backend as bmb  # noqa: E402


def test_save_results_writes_json_and_ansi_stripped_text(tmp_path):
    """save_results() writes <name>_<ts>.json plus an ANSI-stripped .txt log."""
    data = {"benchmark": "mlx_backend", "models": [{"model": "x", "speedup": 2.5}]}

    json_path = bmb.save_results(
        "mlx_backend",
        data,
        file_timestamp="20260101_000000",
        text="line one\n\x1b[32mgreen\x1b[0m line",
        out_dir=str(tmp_path),
    )

    assert json_path == tmp_path / "mlx_backend_20260101_000000.json"
    assert json.loads(json_path.read_text()) == data

    txt_path = tmp_path / "mlx_backend_20260101_000000.txt"
    assert txt_path.exists()
    text = txt_path.read_text()
    assert "green line" in text
    assert "\x1b[" not in text  # ANSI escapes stripped


def test_save_results_skips_txt_when_no_text(tmp_path):
    """No .txt file is written when no console text is provided."""
    bmb.save_results(
        "mlx_backend", {"a": 1}, file_timestamp="20260101_000000", out_dir=str(tmp_path)
    )
    assert not (tmp_path / "mlx_backend_20260101_000000.txt").exists()
