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


def test_eight_parallel_calls_through_the_mcp_client_all_succeed(tmp_path):
    script = f"""
import asyncio, os, sys
sys.path.insert(0, {str(REPO)!r})
from tests._pdfium_race_workload import make_pdf
from fastmcp import Client
import pdf_mcp.server  # noqa: F401
from pdf_mcp import _core

async def main():
    paths = []
    for i in range(4):
        p = os.path.join({str(tmp_path)!r}, f"d{{i}}.pdf")
        make_pdf(p)
        paths.append(p)
    async with Client(_core.mcp) as c:
        calls = []
        for p in paths:
            calls.append(c.call_tool("pdf_info", {{"path": p}}))
            calls.append(
                c.call_tool("pdf_search", {{"path": p, "query": "supply voltage"}})
            )
        results = await asyncio.gather(*calls, return_exceptions=True)
    bad = [r for r in results if isinstance(r, Exception)]
    print("bad:", bad)
    sys.exit(1 if bad else 0)

asyncio.run(main())
"""
    env = dict(os.environ, PDF_MCP_CACHE_DIR=str(tmp_path / "cache"))
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr[-2000:]
