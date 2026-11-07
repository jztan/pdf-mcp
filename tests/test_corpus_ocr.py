"""OCR on corpus warm (issue #103): pending pages, coverage, background
thread, and the pdf_corpus_warm `ocr` parameter."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdf_mcp import corpus
from pdf_mcp.cache import PDFCache
from pdf_mcp.tools import corpus_tools


def _blank_pdf(path: Path, pages: int) -> str:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(200, 200)
    doc.save(str(path))
    doc.close()
    return str(path)


def _cov(*pairs: tuple[int, int]) -> list[dict[str, int]]:
    return [{"text_chars": t, "raster_images": r} for t, r in pairs]


@pytest.fixture
def cache(tmp_path):
    return PDFCache(cache_dir=tmp_path / "cache")


@pytest.fixture
def scan3(tmp_path, cache):
    """Page 0 has text, page 1 is an image, page 2 is blank."""
    path = _blank_pdf(tmp_path / "scan3.pdf", 3)
    cache.save_metadata(path, 3, {}, [], text_coverage=_cov((10, 1), (0, 1), (0, 0)))
    return path


@pytest.fixture
def scan2(tmp_path, cache):
    """Two image-only pages."""
    path = _blank_pdf(tmp_path / "scan2.pdf", 2)
    cache.save_metadata(path, 2, {}, [], text_coverage=_cov((0, 1), (0, 1)))
    cache.save_pages_text(path, {0: "", 1: ""})
    return path


class TestPendingPages:
    def test_candidates_only_textless_image_pages(self):
        from pdf_mcp.corpus_ocr import ocr_candidates

        assert ocr_candidates(_cov((10, 1), (0, 1), (0, 0))) == [1]

    def test_pending_excludes_pages_with_any_ocr_row_for_lang(self, cache, scan2):
        from pdf_mcp.corpus_ocr import ocr_pending_pages

        cache.save_page_text(scan2, 0, "x", source="ocr", ocr_lang="eng")
        cache.save_page_text(scan2, 1, "", source="ocr", ocr_lang="eng")
        assert ocr_pending_pages(scan2, cache, "eng") == []
        assert ocr_pending_pages(scan2, cache, "deu") == [0, 1]

    def test_pending_without_metadata_is_empty(self, cache, tmp_path):
        from pdf_mcp.corpus_ocr import ocr_pending_pages

        path = _blank_pdf(tmp_path / "cold.pdf", 1)
        assert ocr_pending_pages(path, cache, "eng") == []

    def test_progress_counts(self, cache, scan2):
        from pdf_mcp.corpus_ocr import ocr_progress

        cache.save_page_text(scan2, 0, "x", source="ocr", ocr_lang="eng")
        cache.save_page_text(scan2, 1, "", source="ocr", ocr_lang="eng")
        assert ocr_progress(scan2, cache, "eng") == {
            "pending": 0,
            "done": 1,
            "no_text": 1,
        }

    def test_lang_normalised(self, cache, scan2):
        from pdf_mcp.corpus_ocr import ocr_pending_pages

        cache.save_page_text(scan2, 0, "x", source="ocr", ocr_lang="ENG ")
        assert ocr_pending_pages(scan2, cache, "eng") == [1]

    def test_get_ocr_rows_any_language(self, cache, scan2):
        cache.save_page_text(scan2, 0, "", source="ocr", ocr_lang="deu")
        cache.save_page_text(scan2, 0, "hallo", source="ocr", ocr_lang="eng")
        assert cache.get_ocr_rows(scan2, [0, 1], None) == {0: "hallo"}
        assert cache.get_ocr_rows(scan2, [0, 1], "deu") == {0: ""}

    def test_blank_and_text_pages_never_pending(self, cache, scan3):
        from pdf_mcp.corpus_ocr import ocr_pending_pages

        assert ocr_pending_pages(scan3, cache, "eng") == [1]


class TestCoverage:
    def test_effective_coverage_counts_ocr_text(self, cache, scan2):
        from pdf_mcp.corpus_ocr import effective_coverage

        cache.save_page_text(scan2, 1, "abcd", source="ocr", ocr_lang="eng")
        cov = effective_coverage(scan2, cache)
        assert [c["text_chars"] for c in cov] == [0, 4]

    def test_coverage_label_full_after_ocr(self, cache, scan2):
        assert corpus._doc_coverage_label(scan2, cache) == "none"
        cache.save_page_text(scan2, 0, "one", source="ocr", ocr_lang="eng")
        assert corpus._doc_coverage_label(scan2, cache) == "partial"
        cache.save_page_text(scan2, 1, "two", source="ocr", ocr_lang="eng")
        assert corpus._doc_coverage_label(scan2, cache) == "full"

    def test_overview_card_counts_ocr_text(self, cache, scan2):
        cache.save_page_text(scan2, 0, "one", source="ocr", ocr_lang="eng")
        cache.save_page_text(scan2, 1, "two", source="ocr", ocr_lang="eng")
        card = corpus.build_overview_card(scan2, cache, from_cache=True)
        assert card["text_coverage"] == "full"


class TestOcrRowsInCorpusSearch:
    """The spec's #71 claim, proven before anything is built on it: an OCR
    row written by save_page_text is what corpus keyword search reads."""

    def _ocr_scan(self, path):
        from pdf_mcp import _core
        from pdf_mcp.server import pdf_corpus_warm

        warm = pdf_corpus_warm([path])
        assert "error" not in warm
        _core.cache.save_page_text(
            path, 0, "quarterly zebra revenue", source="ocr", ocr_lang="eng"
        )
        return _core.cache

    def test_ocr_row_found_by_corpus_keyword_search(self, sample_pdf_synthetic_scan):
        from pdf_mcp.server import pdf_corpus_search

        self._ocr_scan(sample_pdf_synthetic_scan)
        res = pdf_corpus_search([sample_pdf_synthetic_scan], "zebra", mode="keyword")
        assert [m["page"] for m in res["matches"]] == [1]
        assert res["matches"][0]["path"] == sample_pdf_synthetic_scan

    def test_rewarm_keeps_ocr_row(self, sample_pdf_synthetic_scan):
        from pdf_mcp.server import pdf_corpus_warm

        cache = self._ocr_scan(sample_pdf_synthetic_scan)
        pdf_corpus_warm([sample_pdf_synthetic_scan])
        assert cache.get_ocr_rows(sample_pdf_synthetic_scan, [0], "eng") == {
            0: "quarterly zebra revenue"
        }

    def test_low_text_coverage_clears_after_ocr(self, sample_pdf_synthetic_scan):
        from pdf_mcp.server import pdf_corpus_search

        self._ocr_scan(sample_pdf_synthetic_scan)
        res = pdf_corpus_search([sample_pdf_synthetic_scan], "zebra", mode="keyword")
        assert sample_pdf_synthetic_scan not in (res.get("low_text_coverage") or {})


@pytest.fixture
def ocr_thread_on(monkeypatch):
    monkeypatch.setenv("PDF_MCP_BACKGROUND_OCR", "1")


def _fake_worker(calls=None, result=None):
    def worker(args):
        _path, pn = args[0], args[1]
        if calls is not None:
            calls.append((_path, pn))
        if result is not None:
            return pn, result
        return pn, f"text {pn}"

    return worker


class TestBackgroundOCR:
    def test_ocr_writes_rows_then_nothing_pending(self, cache, scan2, ocr_thread_on):
        from pdf_mcp.corpus_ocr import BackgroundOCR, ocr_pending_pages

        bg = BackgroundOCR()
        jobs = [(scan2, ocr_pending_pages(scan2, cache, "eng"))]
        assert bg.start(jobs, cache, "eng", None, worker=_fake_worker()) == 2
        assert bg.join(10)
        assert ocr_pending_pages(scan2, cache, "eng") == []
        assert cache.get_ocr_rows(scan2, [0, 1], "eng") == {0: "text 0", 1: "text 1"}

    def test_empty_result_is_recorded_once(self, cache, scan2, ocr_thread_on):
        from pdf_mcp.corpus_ocr import BackgroundOCR, ocr_pending_pages, ocr_progress
        from pdf_mcp.parallel import PageError

        bg = BackgroundOCR()
        jobs = [(scan2, ocr_pending_pages(scan2, cache, "eng"))]
        bg.start(jobs, cache, "eng", None, worker=_fake_worker(result=PageError("x")))
        assert bg.join(10)
        assert ocr_progress(scan2, cache, "eng") == {
            "pending": 0,
            "done": 0,
            "no_text": 2,
        }
        jobs = [(scan2, ocr_pending_pages(scan2, cache, "eng"))]
        assert BackgroundOCR().start(jobs, cache, "eng", None) == 0

    def test_stop_drops_inflight_batch(self, cache, scan2, ocr_thread_on):
        import threading

        from pdf_mcp.corpus_ocr import BackgroundOCR

        entered, release = threading.Event(), threading.Event()

        def blocking(args):
            entered.set()
            release.wait(10)
            return args[1], "late"

        bg = BackgroundOCR()
        bg.start([(scan2, [0, 1])], cache, "eng", None, worker=blocking)
        assert entered.wait(10)
        bg.stop()
        release.set()
        assert bg.join(10)
        assert cache.get_ocr_rows(scan2, [0, 1], None) == {}

    def test_restart_resumes_from_cache(self, cache, scan2, ocr_thread_on):
        from pdf_mcp.corpus_ocr import BackgroundOCR, ocr_pending_pages

        cache.save_page_text(scan2, 0, "already", source="ocr", ocr_lang="eng")
        calls: list = []
        bg = BackgroundOCR()
        jobs = [(scan2, ocr_pending_pages(scan2, cache, "eng"))]
        bg.start(jobs, cache, "eng", None, worker=_fake_worker(calls))
        assert bg.join(10)
        assert calls == [(scan2, 1)]

    def test_skips_pages_written_meanwhile(self, cache, scan2, ocr_thread_on):
        from pdf_mcp.corpus_ocr import BackgroundOCR

        cache.save_page_text(scan2, 1, "by read_pages", source="ocr", ocr_lang="eng")
        calls: list = []
        bg = BackgroundOCR()
        bg.start([(scan2, [0, 1])], cache, "eng", None, worker=_fake_worker(calls))
        assert bg.join(10)
        assert calls == [(scan2, 0)]
        assert cache.get_ocr_rows(scan2, [1], "eng") == {1: "by read_pages"}

    def test_fewest_pending_first(self, cache, tmp_path, ocr_thread_on):
        from pdf_mcp.corpus_ocr import BackgroundOCR

        big = _blank_pdf(tmp_path / "a_big.pdf", 3)
        cache.save_metadata(big, 3, {}, [], text_coverage=_cov((0, 1), (0, 1), (0, 1)))
        small = _blank_pdf(tmp_path / "z_small.pdf", 1)
        cache.save_metadata(small, 1, {}, [], text_coverage=_cov((0, 1)))
        calls: list = []
        bg = BackgroundOCR()
        bg.start(
            [(big, [0, 1, 2]), (small, [0])],
            cache,
            "eng",
            None,
            worker=_fake_worker(calls),
        )
        assert bg.join(10)
        assert calls[0] == (small, 0)

    def test_disabled_by_env(self, cache, scan2, monkeypatch):
        from pdf_mcp.corpus_ocr import BackgroundOCR

        monkeypatch.setenv("PDF_MCP_BACKGROUND_OCR", "0")
        bg = BackgroundOCR()
        assert bg.start([(scan2, [0, 1])], cache, "eng", None) == 0
        assert not bg.running("eng")

    def test_running_is_per_language(self, cache, scan2, ocr_thread_on):
        import threading

        from pdf_mcp.corpus_ocr import BackgroundOCR

        release = threading.Event()

        def blocking(args):
            release.wait(10)
            return args[1], "x"

        bg = BackgroundOCR()
        bg.start([(scan2, [0, 1])], cache, "eng", None, worker=blocking)
        assert bg.running("eng") and not bg.running("deu")
        bg.stop()
        release.set()
        assert bg.join(10)

    def test_cache_clear_stops_it(self, isolated_server, monkeypatch):
        from pdf_mcp import corpus_ocr
        from pdf_mcp.server import pdf_cache_clear

        stopped = []
        monkeypatch.setattr(
            corpus_ocr.BACKGROUND_OCR, "stop", lambda: stopped.append(True)
        )
        pdf_cache_clear(expired_only=False)
        assert stopped == [True]


@pytest.fixture
def ocr_ok(monkeypatch):
    """Tesseract usable; the pool worker is a fake that reads a fixed phrase."""
    from pdf_mcp import corpus_ocr

    monkeypatch.setattr(corpus_tools, "_ocr_unavailable", lambda lang: None)
    monkeypatch.setattr(
        corpus_ocr, "_ocr_page_worker", lambda args: (args[1], "zebra phrase")
    )


class TestWarmOcrParameter:
    def test_off_is_byte_identical(self, corpus_dir, isolated_server):
        from pdf_mcp.server import pdf_corpus_warm

        res = pdf_corpus_warm(str(corpus_dir), ocr="off")
        # The response keys of v3.7.0, unchanged by issue #103.
        assert set(res) == {
            "budget_exhausted",
            "cached_docs",
            "cached_without_embeddings",
            "corpus_size",
            "docs",
            "embedding_in_background",
            "skipped",
            "unprocessed",
            "unwarmed",
            "warm_complete",
            "warmed_this_call",
        }
        assert all("ocr_pages_pending" not in d for d in res["docs"])

    def test_born_digital_auto_not_needed(self, corpus_dir, isolated_server, ocr_ok):
        from pdf_mcp.server import pdf_corpus_warm

        res = pdf_corpus_warm(str(corpus_dir))
        assert res["ocr"]["status"] == "not_needed"
        assert res["ocr"]["pages_pending"] == 0
        assert res["warm_complete"] is True

    def test_scan_auto_running_then_done(
        self, sample_pdf_synthetic_scan, ocr_ok, ocr_thread_on
    ):
        from pdf_mcp import corpus_ocr
        from pdf_mcp.server import pdf_corpus_search, pdf_corpus_warm

        first = pdf_corpus_warm([sample_pdf_synthetic_scan])
        assert first["ocr"]["status"] == "running"
        assert first["ocr"]["pages_pending"] == 1
        assert first["ocr"]["hint"]
        assert first["warm_complete"] is False
        assert corpus_ocr.BACKGROUND_OCR.join(10)

        again = pdf_corpus_warm([sample_pdf_synthetic_scan])
        assert again["ocr"]["status"] == "done"
        assert again["ocr"]["pages_done"] == 1
        assert again["warm_complete"] is True
        hits = pdf_corpus_search([sample_pdf_synthetic_scan], "zebra", mode="keyword")
        assert [m["page"] for m in hits["matches"]] == [1]

    def test_on_without_tesseract_is_inline_error(
        self, sample_pdf_synthetic_scan, monkeypatch, ocr_thread_on
    ):
        from pdf_mcp import _core, corpus_ocr
        from pdf_mcp.server import pdf_corpus_warm

        monkeypatch.setattr(
            corpus_tools, "_ocr_unavailable", lambda lang: {"error": "no tesseract"}
        )
        res = pdf_corpus_warm([sample_pdf_synthetic_scan], ocr="on")
        assert res == {"error": "no tesseract"}
        assert not corpus_ocr.BACKGROUND_OCR.running("eng")
        assert _core.cache.get_metadata(sample_pdf_synthetic_scan) is None

    def test_auto_without_tesseract_reports_unavailable_and_completes(
        self, sample_pdf_synthetic_scan, monkeypatch, ocr_thread_on
    ):
        from pdf_mcp import corpus_ocr
        from pdf_mcp.server import pdf_corpus_warm

        monkeypatch.setattr(
            corpus_tools,
            "_ocr_unavailable",
            lambda lang: {"error": "no tesseract", "install_hint": "brew install"},
        )
        res = pdf_corpus_warm([sample_pdf_synthetic_scan])
        assert res["ocr"]["status"] == "unavailable"
        assert "brew install" in res["ocr"]["hint"]
        assert res["warm_complete"] is True
        assert not corpus_ocr.BACKGROUND_OCR.running("eng")

    def test_setting_up_keeps_warm_incomplete(
        self, sample_pdf_synthetic_scan, monkeypatch, ocr_thread_on
    ):
        from pdf_mcp.server import pdf_corpus_warm

        monkeypatch.setattr(
            corpus_tools,
            "_ocr_unavailable",
            lambda lang: {"error": "Setting up OCR (a one-time download)."},
        )
        res = pdf_corpus_warm([sample_pdf_synthetic_scan])
        assert res["ocr"]["status"] == "setting_up"
        assert res["warm_complete"] is False

    def test_warm_calls_do_not_restart_running_ocr(
        self, sample_pdf_synthetic_scan, monkeypatch, ocr_thread_on
    ):
        import threading

        from pdf_mcp import _core, corpus_ocr
        from pdf_mcp.server import pdf_corpus_warm

        monkeypatch.setattr(corpus_tools, "_ocr_unavailable", lambda lang: None)
        release = threading.Event()
        calls: list = []

        def blocking(args):
            calls.append(args[1])
            release.wait(10)
            return args[1], "zebra"

        monkeypatch.setattr(corpus_ocr, "_ocr_page_worker", blocking)
        for _ in range(3):
            res = pdf_corpus_warm([sample_pdf_synthetic_scan])
            assert res["ocr"]["status"] == "running"
        release.set()
        assert corpus_ocr.BACKGROUND_OCR.join(10)
        assert calls == [0]
        assert _core.cache.get_ocr_rows(sample_pdf_synthetic_scan, [0], "eng") == {
            0: "zebra"
        }

    def test_invalid_ocr_value_is_inline_error(self, corpus_dir, isolated_server):
        from pdf_mcp.server import pdf_corpus_warm

        res = pdf_corpus_warm(str(corpus_dir), ocr="maybe")
        assert "error" in res and "hint" in res

    def test_detail_rows_carry_pending(
        self, sample_pdf_synthetic_scan, monkeypatch, ocr_thread_on
    ):
        from pdf_mcp.server import pdf_corpus_warm

        monkeypatch.setattr(
            corpus_tools, "_ocr_unavailable", lambda lang: {"error": "no tesseract"}
        )
        res = pdf_corpus_warm([sample_pdf_synthetic_scan], detail=True)
        assert [d["ocr_pages_pending"] for d in res["docs"]] == [1]


class TestReviewFixes:
    """Final-review findings on the #103 branch, each pinned before its fix."""

    def test_inprocess_ocr_holds_pdf_lock(self, cache, scan2, ocr_thread_on):
        # Critical 1: with one worker, OCR runs in the server process and
        # drives pdfium, so it must hold PDF_ACCESS like any tool call.
        from pdf_mcp.concurrency import PDF_ACCESS
        from pdf_mcp.corpus_ocr import BackgroundOCR

        held: list[bool] = []

        def worker(args):
            held.append(PDF_ACCESS.held_by_current_thread())
            return args[1], "x"

        bg = BackgroundOCR()
        bg.start([(scan2, [0, 1])], cache, "eng", None, worker=worker)
        assert bg.join(10)
        assert held == [True, True]

    def test_single_page_batch_uses_the_pool_when_workers_allowed(self, monkeypatch):
        # Critical 1: worker count comes from the batch cap, not the batch
        # size, so a one-page batch never falls back to in-process OCR.
        from pdf_mcp.corpus_ocr import ocr_worker_count

        monkeypatch.delenv("PDF_MCP_MAX_WORKERS", raising=False)
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        assert ocr_worker_count() == 4
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")
        assert ocr_worker_count() == 1

    def test_bare_page_error_is_recorded(self, cache, scan2, ocr_thread_on):
        # Important 3: run_pages' bounded fallback returns a bare PageError.
        from pdf_mcp.corpus_ocr import BackgroundOCR, ocr_progress
        from pdf_mcp.parallel import PageError

        bg = BackgroundOCR()
        bg.start([(scan2, [0, 1])], cache, "eng", None, worker=lambda a: PageError("t"))
        assert bg.join(10)
        assert ocr_progress(scan2, cache, "eng")["no_text"] == 2

    def test_ocr_lengths_stat_the_file_once(self, cache, scan2, monkeypatch):
        # Important 4: one stat per call, not one per row.
        cache.save_page_text(scan2, 0, "abc", source="ocr", ocr_lang="eng")
        cache.save_page_text(scan2, 1, "", source="ocr", ocr_lang="eng")
        calls = []
        real = cache._get_file_info
        monkeypatch.setattr(
            cache, "_get_file_info", lambda p: calls.append(p) or real(p)
        )
        assert cache.get_ocr_text_lengths(scan2, [0, 1], "eng") == {0: 3, 1: 0}
        assert len(calls) == 1

    def test_new_jobs_join_the_running_thread(
        self, cache, scan2, tmp_path, ocr_thread_on
    ):
        # Important 5: a second corpus is queued, not starved or restarted.
        import threading

        from pdf_mcp.corpus_ocr import BackgroundOCR

        other = _blank_pdf(tmp_path / "other.pdf", 1)
        cache.save_metadata(other, 1, {}, [], text_coverage=_cov((0, 1)))
        release = threading.Event()
        seen: list = []

        def worker(args):
            seen.append((args[0], args[1]))
            release.wait(10)
            return args[1], "t"

        bg = BackgroundOCR()
        bg.start([(scan2, [0, 1])], cache, "eng", None, worker=worker)
        assert bg.start([(other, [0]), (scan2, [0, 1])], cache, "ENG", None) == 1
        release.set()
        assert bg.join(10)
        assert sorted(seen) == sorted([(scan2, 0), (scan2, 1), (other, 0)])
        assert cache.get_ocr_rows(other, [0], "eng") == {0: "t"}

    def test_stop_shuts_down_the_pool(self):
        # Important 6: exit must not wait for an in-flight batch.
        from pdf_mcp.corpus_ocr import BackgroundOCR

        calls = []

        class FakePool:
            _processes: dict = {}

            def shutdown(self, wait=True, cancel_futures=False):
                calls.append((wait, cancel_futures))

        bg = BackgroundOCR()
        bg._pool = FakePool()
        bg.stop()
        assert calls == [(False, True)]
        assert bg._pool is None

    def test_no_write_after_stop_mid_batch(self, cache, scan2, ocr_thread_on):
        # Important 7: a stop (cache clear) between pages ends the writes.
        from pdf_mcp.corpus_ocr import BackgroundOCR

        bg = BackgroundOCR()

        def worker(args):
            if args[1] == 1:
                bg.stop()
            return args[1], f"t{args[1]}"

        bg.start([(scan2, [0, 1])], cache, "eng", None, worker=worker)
        assert bg.join(10)
        assert cache.get_ocr_rows(scan2, [0, 1], "eng") == {0: "t0"}

    def test_disabled_thread_does_not_hold_warm_open(
        self, sample_pdf_synthetic_scan, monkeypatch
    ):
        # Important 2: PDF_MCP_BACKGROUND_OCR=0 must not loop agents forever.
        from pdf_mcp.server import pdf_corpus_warm

        monkeypatch.setattr(corpus_tools, "_ocr_unavailable", lambda lang: None)
        monkeypatch.setenv("PDF_MCP_BACKGROUND_OCR", "0")
        res = pdf_corpus_warm([sample_pdf_synthetic_scan])
        assert res["ocr"]["status"] == "unavailable"
        assert "PDF_MCP_BACKGROUND_OCR" in res["ocr"]["hint"]
        assert res["warm_complete"] is True
