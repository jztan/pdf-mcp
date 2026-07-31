"""The demo page's JS fusion port must match the Python reference.

Runs the Node parity checker (scripts/check_demo_fusion_parity.mjs)
against committed fixtures. Skips when node is not installed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_demo_fusion_parity() -> None:
    proc = subprocess.run(
        ["node", str(REPO / "scripts" / "check_demo_fusion_parity.mjs")],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
