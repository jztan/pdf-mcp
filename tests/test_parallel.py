"""Unit tests for the process-pool helper (pdf_mcp.parallel)."""

import subprocess
import sys
import time

from pdf_mcp.parallel import PageError, resolve_workers, run_pages


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


def _square(n):
    return n * n


def _sleep_worker(n):
    # Sleeps far longer than any test timeout so the bound must kill it.
    time.sleep(30)
    return n


def _raise_worker(n):
    raise RuntimeError(f"boom {n}")


class TestBoundedHelpers:
    def test_overall_timeout_scales_with_waves(self):
        from pdf_mcp.parallel import _overall_timeout

        assert _overall_timeout(5, 2, 10) == 30  # ceil(5/2) = 3 waves
        assert _overall_timeout(2, 8, 10) == 10  # single wave
        assert _overall_timeout(0, 8, 10) == 0
        assert _overall_timeout(3, 0, 10) == 30  # guards div-by-zero

    def test_run_page_bounded_returns_result(self):
        from pdf_mcp.parallel import _run_page_bounded

        assert _run_page_bounded(_square, 6, page_timeout=10) == 36

    def test_run_page_bounded_kills_hung_worker(self):
        from pdf_mcp.parallel import _run_page_bounded, PageError

        t0 = time.monotonic()
        res = _run_page_bounded(_sleep_worker, 7, page_timeout=1)
        elapsed = time.monotonic() - t0
        assert isinstance(res, PageError)
        # Bounded to ~page_timeout: killed, not waited out (30s worker).
        assert elapsed < 5

    def test_run_page_bounded_captures_worker_exception(self):
        from pdf_mcp.parallel import _run_page_bounded, PageError

        res = _run_page_bounded(_raise_worker, 3, page_timeout=10)
        assert isinstance(res, PageError)
        assert "boom 3" in res.detail


class TestRunPages:
    def test_sequential_branch_preserves_order(self):
        # max_workers <= 1 -> no pool
        out = run_pages(_square, [1, 2, 3, 4], max_workers=1)
        assert out == [1, 4, 9, 16]

    def test_real_pool_preserves_order(self):
        out = run_pages(_square, [1, 2, 3, 4, 5], max_workers=2)
        assert out == [1, 4, 9, 16, 25]

    def test_broken_pool_falls_back_to_bounded_subprocess(self, monkeypatch):
        from concurrent.futures.process import BrokenProcessPool

        class _BoomPool:
            def __init__(self, *a, **k):
                pass

            def shutdown(self, **k):
                pass

            def submit(self, worker, arg):
                raise BrokenProcessPool("worker died")

        monkeypatch.setattr("pdf_mcp.parallel.ProcessPoolExecutor", _BoomPool)
        # Pool submit raises -> every page recovered via the bounded
        # subprocess path (real multiprocessing), still correct + ordered.
        out = run_pages(_square, [2, 3, 4], max_workers=4)
        assert out == [4, 9, 16]

    def test_hung_worker_does_not_hang_run_pages(self):
        from pdf_mcp.parallel import PageError

        t0 = time.monotonic()
        out = run_pages(_sleep_worker, [1], max_workers=2, page_timeout=1)
        elapsed = time.monotonic() - t0
        assert isinstance(out[0], PageError)
        # overall pool wait (~1s) + bounded fallback (~1s); killed, not waited.
        assert elapsed < 8
