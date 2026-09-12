"""Run the launcher's node test suite as part of pytest."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_launcher_node_suite():
    # Explicit files: Node 22+ no longer accepts a bare directory here, and
    # Node 18/20 do not expand glob patterns, so a file list works on all.
    files = sorted(str(p) for p in (ROOT / "tests" / "launcher").glob("*.test.js"))
    result = subprocess.run(
        ["node", "--test", *files],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
