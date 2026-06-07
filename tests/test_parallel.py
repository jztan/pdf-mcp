"""Unit tests for the process-pool helper (pdf_mcp.parallel)."""

import subprocess
import sys


def test_importing_extractor_does_not_import_server():
    # Spawn-safety: a worker imports pdf_mcp.extractor to unpickle; that must
    # NOT drag in server.py / FastMCP / a module-level PDFCache. Run in a fresh
    # interpreter so this test is not polluted by other imports in-process.
    code = (
        "import sys, pdf_mcp.extractor;"
        " assert 'pdf_mcp.server' not in sys.modules, 'server imported';"
        " assert 'fastmcp' not in sys.modules, 'fastmcp imported';"
        " print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout


def test_package_mcp_still_accessible():
    # Lazy access must still work, via both attribute and from-import forms.
    import pdf_mcp
    from pdf_mcp import mcp

    assert pdf_mcp.mcp is not None
    assert mcp is pdf_mcp.mcp
