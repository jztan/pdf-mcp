"""Parallel tool calls must not crash or poison the PDF engine.

The workload runs in a subprocess: an unfixed pdfium race is a SIGSEGV,
which has to fail this test, not end the pytest session.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _run_workload(tmp_path, *flags):
    env = dict(os.environ, PDF_MCP_CACHE_DIR=str(tmp_path / "cache"))
    return subprocess.run(
        [sys.executable, "-m", "tests._pdfium_race_workload", *flags],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_parallel_tool_calls_neither_crash_nor_poison_the_engine(tmp_path):
    result = _run_workload(tmp_path)
    assert result.returncode == 0, (
        f"exit {result.returncode}\nstdout:\n{result.stdout}\n"
        f"stderr (tail):\n{result.stderr[-2000:]}"
    )
