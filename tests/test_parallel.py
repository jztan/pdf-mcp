"""Unit tests for the process-pool helper (pdf_mcp.parallel)."""

import subprocess
import sys

from pdf_mcp.parallel import PageError, resolve_workers


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


class TestPageError:
    def test_carries_detail_and_repr(self):
        err = PageError("ValueError('bad page')")
        assert err.detail == "ValueError('bad page')"
        assert "bad page" in repr(err)


class TestResolveWorkers:
    def test_below_gate_returns_one(self):
        # 1 miss page, gate 2 -> sequential
        assert resolve_workers(1, gate=2, cap=8) == 1

    def test_at_gate_parallelizes(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 8)
        assert resolve_workers(2, gate=2, cap=8) == 2

    def test_clamped_by_n_pages(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 8)
        assert resolve_workers(3, gate=2, cap=8) == 3

    def test_clamped_by_cap(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 32)
        assert resolve_workers(100, gate=2, cap=8) == 8

    def test_cpu_count_none_falls_back_to_one(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: None)
        assert resolve_workers(100, gate=2, cap=8) == 1

    def test_env_zero_forces_sequential(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 8)
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "0")
        assert resolve_workers(100, gate=2, cap=8) == 1

    def test_env_one_forces_sequential(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 8)
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")
        assert resolve_workers(100, gate=2, cap=8) == 1

    def test_env_caps_down(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 8)
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "3")
        assert resolve_workers(100, gate=2, cap=8) == 3

    def test_env_cannot_exceed_cap(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 8)
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "100")
        assert resolve_workers(100, gate=2, cap=8) == 8

    def test_env_cannot_raise_above_cpu_computed(self, monkeypatch):
        # env between the cpu-computed value and cap must NOT raise the result
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 4)
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "6")
        assert resolve_workers(100, gate=2, cap=8) == 4

    def test_negative_env_forces_sequential(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 8)
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "-2")
        assert resolve_workers(100, gate=2, cap=8) == 1

    def test_invalid_env_is_ignored(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 8)
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "not-a-number")
        assert resolve_workers(100, gate=2, cap=8) == 8
