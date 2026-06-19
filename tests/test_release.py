# tests/test_release.py
"""Tests for scripts/release.py pre-flight behavior."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import release  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent


def test_preflight_pytest_cmd_deselects_slow_tests():
    """The release gate must not run billed/slow tests (e.g. the coherence
    eval that shells out to the real `claude` CLI over the corpus)."""
    cmd = release.preflight_pytest_cmd()
    assert "tests/" in cmd
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "not slow"


def test_slow_marker_deselects_coherence_eval():
    """`-m "not slow"` must actually exclude the billed coherence guard.

    Real behavior check: collect test_eval_coherence.py with the gate's marker
    filter and confirm the billed node is not collected."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_eval_coherence.py",
            "-m",
            "not slow",
            "--collect-only",
            "-q",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert "test_coherence_no_regression_vs_baseline" not in result.stdout
