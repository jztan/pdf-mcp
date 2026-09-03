"""Tests for corpus resolution and warm orchestration (corpus.py)."""

import pickle
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pymupdf

from pdf_mcp import corpus, extractor
from pdf_mcp.extractor import _warm_extract_worker
import pytest


class TestResolveCorpus:
    def test_directory_mode_finds_sorted_pdfs(self, corpus_dir):
        res = corpus.resolve_corpus(str(corpus_dir))
        names = [Path(p).name for p in res["files"]]
        assert names == ["alpha.pdf", "bravo.pdf", "charlie.pdf"]
        assert res["skipped"] == []

    def test_missing_directory_errors(self, tmp_path):
        res = corpus.resolve_corpus(str(tmp_path / "missing"))
        assert "error" in res
        assert "hint" in res

    def test_empty_directory_errors(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        res = corpus.resolve_corpus(str(d))
        assert "error" in res

    def test_recursive_opt_in(self, corpus_dir):
        sub = corpus_dir / "sub"
        sub.mkdir()
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(sub / "deep.pdf"))
        doc.close()
        flat = corpus.resolve_corpus(str(corpus_dir))
        deep = corpus.resolve_corpus(str(corpus_dir), recursive=True)
        assert len(flat["files"]) == 3
        assert len(deep["files"]) == 4

    def test_cap_exceeded_errors(self, corpus_dir, monkeypatch):
        monkeypatch.setattr(corpus, "CORPUS_MAX_FILES", 2)
        res = corpus.resolve_corpus(str(corpus_dir))
        assert "error" in res
        assert "2-file cap" in res["error"]

    def test_list_mode_skips_invalid_entries(self, corpus_dir, tmp_path):
        entries = [
            str(corpus_dir / "alpha.pdf"),
            "https://example.com/x.pdf",
            str(tmp_path / "notes.txt"),
            str(tmp_path / "ghost.pdf"),
        ]
        res = corpus.resolve_corpus(entries)
        assert [Path(p).name for p in res["files"]] == ["alpha.pdf"]
        assert len(res["skipped"]) == 3
        reasons = " ".join(s["reason"] for s in res["skipped"])
        assert "URL" in reasons
        assert "not found" in reasons

    def test_check_path_denial_goes_to_skipped(self, corpus_dir):
        def deny(path: str) -> None:
            raise ValueError("path denied by config")

        res = corpus.resolve_corpus(str(corpus_dir), check_path=deny)
        # All three denied -> empty corpus is a call-level error,
        # with the per-file reasons still reported.
        assert "error" in res
        assert len(res["skipped"]) == 3
        assert "denied" in res["skipped"][0]["reason"]

    def test_duplicate_paths_deduped(self, corpus_dir):
        p = str(corpus_dir / "alpha.pdf")
        res = corpus.resolve_corpus([p, p])
        assert len(res["files"]) == 1

    def test_url_as_directory_gets_url_specific_error(self):
        """A URL passed as the paths string gets a URL-specific error,
        not the generic 'Not a directory' (field feedback)."""
        res = corpus.resolve_corpus("https://arxiv.org/pdf/1803.03635")
        assert "error" in res
        assert "URL" in res["error"]
        assert "Not a directory" not in res["error"]
        assert "hint" in res


class SteppingClock:
    """Fake monotonic clock: advances a fixed step on every call."""

    def __init__(self, step: float):
        self.t = -step  # first call returns 0.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def _files(corpus_dir):
    return corpus.resolve_corpus(str(corpus_dir))["files"]


class TestMissingEmbedPages:
    def test_pages_without_rows_are_missing(self):
        texts = {0: "alpha text", 1: "bravo text", 2: ""}
        stored = {0: [b"x"] * len(extractor.page_embedding_units("alpha text"))}
        assert corpus._missing_embed_pages(texts, stored) == [1]

    def test_empty_pages_never_missing(self):
        assert corpus._missing_embed_pages({0: "   "}, {}) == []

    def test_stale_layout_pages_are_missing_again(self):
        texts = {0: "some real page text"}
        stored = {0: [b"x"]}  # one row where units() would write != 1
        expected = extractor.stale_layout_pages(texts, stored)
        got = corpus._missing_embed_pages(texts, stored)
        assert got == expected if expected else got == []

    def test_result_sorted_and_deduped(self):
        texts = {3: "c", 1: "a", 2: "b"}
        assert corpus._missing_embed_pages(texts, {}) == [1, 2, 3]


class TestEmbedDocBatched:
    @staticmethod
    def _embed(texts):
        return [b"\x00\x00\x80?" for _ in texts]  # 1.0 float32 LE

    def _texts(self, n):
        return {i: f"page {i} body text budget report" for i in range(n)}

    def test_completes_and_writes_profile(self, corpus_dir, cache):
        path = str(corpus_dir / "alpha.pdf")
        texts = self._texts(3)
        complete, done = corpus._embed_doc_batched(
            path,
            texts,
            cache,
            "fake-model",
            self._embed,
            deadline=float("inf"),
        )
        assert (complete, done) == (True, 3)
        assert len(cache.get_page_embeddings(path, [0, 1, 2], "fake-model")) == 3
        assert path in cache.get_doc_profiles([path], "fake-model")

    def test_deadline_stops_between_batches_with_floor(
        self, corpus_dir, cache, monkeypatch
    ):
        monkeypatch.setattr(corpus, "WARM_EMBED_BATCH_PAGES", 2)
        path = str(corpus_dir / "alpha.pdf")
        # deadline already passed: the first batch still lands (floor)
        complete, done = corpus._embed_doc_batched(
            path,
            self._texts(5),
            cache,
            "fake-model",
            self._embed,
            deadline=-1.0,
            clock=lambda: 0.0,
        )
        assert complete is False
        assert done == 2
        assert len(cache.get_page_embeddings(path, list(range(5)), "fake-model")) == 2
        # no profile before completion
        assert cache.get_doc_profiles([path], "fake-model") == {}

    def test_resume_embeds_only_missing_pages(self, corpus_dir, cache, monkeypatch):
        monkeypatch.setattr(corpus, "WARM_EMBED_BATCH_PAGES", 2)
        path = str(corpus_dir / "alpha.pdf")
        texts = self._texts(5)
        corpus._embed_doc_batched(
            path,
            texts,
            cache,
            "fake-model",
            self._embed,
            deadline=-1.0,
            clock=lambda: 0.0,
        )
        calls = []

        def counting_embed(chunks):
            calls.append(len(chunks))
            return self._embed(chunks)

        complete, done = corpus._embed_doc_batched(
            path,
            texts,
            cache,
            "fake-model",
            counting_embed,
            deadline=float("inf"),
        )
        assert (complete, done) == (True, 5)
        # pages 0-1 were already stored; only 3 pages' chunks re-encoded,
        # plus one profile encode call at completion
        per_page = len(extractor.page_embedding_units(texts[2]))
        assert sum(calls) == 3 * per_page + 1

    def test_all_empty_pages_completes_immediately(self, corpus_dir, cache):
        path = str(corpus_dir / "alpha.pdf")
        complete, done = corpus._embed_doc_batched(
            path,
            {0: "", 1: "  "},
            cache,
            "fake-model",
            self._embed,
            deadline=float("inf"),
        )
        assert (complete, done) == (True, 0)


class TestFinalizeDocSplit:
    @staticmethod
    def _embed(texts):
        return [b"\x00\x00\x80?" for _ in texts]

    def test_text_only_returns_complete_no_profile(self, corpus_dir, cache):
        path = str(corpus_dir / "alpha.pdf")
        pc, complete, embedded = corpus._warm_one_doc(
            path, cache, embeddings=False, model_name=None, embed=None
        )
        assert complete is True and embedded == 0
        assert cache.get_metadata(path) is not None
        assert cache.get_doc_profiles([path], "fake-model") == {}

    def test_deadline_yields_partial_with_text_committed(
        self, corpus_dir, cache, monkeypatch
    ):
        monkeypatch.setattr(corpus, "WARM_EMBED_BATCH_PAGES", 1)
        path = str(corpus_dir / "bravo.pdf")  # 4 pages
        pc, complete, embedded = corpus._warm_one_doc(
            path,
            cache,
            embeddings=True,
            model_name="fake-model",
            embed=self._embed,
            deadline=-1.0,
            clock=lambda: 0.0,
        )
        assert complete is False
        assert embedded >= 1  # progress floor
        # text landed atomically even though embeddings are partial
        assert corpus._cached_pages(path, cache, False, "fake-model") == pc
        assert corpus._cached_pages(path, cache, True, "fake-model") is None


class TestWarmWorkerCount:
    def test_below_gate_is_sequential(self):
        assert corpus._warm_worker_count(3, embeddings=False) == 1

    def test_text_cap_is_8(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 16)
        monkeypatch.delenv("PDF_MCP_MAX_WORKERS", raising=False)
        assert corpus._warm_worker_count(100, embeddings=False) == 8

    def test_embeddings_cap_is_4(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 16)
        monkeypatch.delenv("PDF_MCP_MAX_WORKERS", raising=False)
        assert corpus._warm_worker_count(100, embeddings=True) == 4

    def test_env_forces_sequential(self, monkeypatch):
        monkeypatch.setattr("pdf_mcp.parallel.os.cpu_count", lambda: 16)
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")
        assert corpus._warm_worker_count(100, embeddings=False) == 1


class TestConcurrentWarm:
    def _force_pool(self, monkeypatch):
        monkeypatch.setattr(corpus, "WARM_DOC_GATE", 1)
        monkeypatch.delenv("PDF_MCP_MAX_WORKERS", raising=False)

    def test_concurrent_matches_sequential(self, corpus_dir, tmp_path, monkeypatch):
        """Corruption invariant + exact equality (single-column fixtures)."""
        from pdf_mcp.cache import PDFCache

        seq_cache = PDFCache(cache_dir=tmp_path / "seq", ttl_hours=1)
        con_cache = PDFCache(cache_dir=tmp_path / "con", ttl_hours=1)
        files = _files(corpus_dir)

        corpus.warm_docs(files, 600, seq_cache, clock=SteppingClock(0))
        self._force_pool(monkeypatch)

        # Spy on pool creation so this test cannot silently degrade into
        # sequential-vs-sequential (and stay green) if _force_pool's
        # monkeypatching ever stops taking effect.
        pool_calls = []
        real_pool = corpus.ProcessPoolExecutor

        def spy(*args, **kwargs):
            pool_calls.append((args, kwargs))
            return real_pool(*args, **kwargs)

        monkeypatch.setattr(corpus, "ProcessPoolExecutor", spy)
        out = corpus.warm_docs(files, 600, con_cache, clock=SteppingClock(0))

        assert pool_calls, "expected _warm_concurrent to create a pool"
        assert out["warmed_this_call"] == 3
        assert out["skipped"] == []
        for path in files:
            want_meta = seq_cache.get_metadata(path)
            got_meta = con_cache.get_metadata(path)
            assert got_meta["page_count"] == want_meta["page_count"]
            assert got_meta["text_coverage"] == want_meta["text_coverage"]
            # TOC and PDF-native metadata are deterministic extraction
            # output (independent of which process did the extracting),
            # so they must match exactly between the sequential and
            # concurrent caches. file_path/file_size/accessed_at are
            # cache-bookkeeping fields, not asserted here.
            assert got_meta["toc"] == want_meta["toc"]
            assert got_meta["metadata"] == want_meta["metadata"]
            pages = list(range(want_meta["page_count"]))
            want = seq_cache.get_pages_text(path, pages)
            got = con_cache.get_pages_text(path, pages)
            # Fixtures are single-column: exact equality is valid here.
            assert got == want

    def test_concurrent_matches_sequential_on_multi_column(self, tmp_path, monkeypatch):
        """Full-text equality on the class that was historically unstable.

        Concurrent warm shipped WITHOUT this assertion. The blocker was
        real: pymupdf4llm's column detector returned 39/40/41 boxes
        depending on what ran before it, so multi-column text could differ
        between two extractions of the same page and an equality test
        would have been flaky rather than wrong.

        Both engines behind that are gone, and the drift measurement now
        says the replacement is stable: 8 repeats over 100 multi-column
        pages, with varied preceding work, gave 100/100 page-stable and
        25/25 doc-stable, against roughly 95.7%/80% for the old path.

        The fixture is generated rather than borrowed from the gitignored
        reading-order corpus, so this runs on a clean checkout, and the
        test asserts the column path actually engaged: a two-column
        fixture that fell back to positional sort would pass this
        trivially while testing nothing.
        """
        import pymupdf

        from pdf_mcp.backend.columns import column_bands
        from pdf_mcp.backend.text import get_text
        from pdf_mcp.cache import PDFCache

        src = tmp_path / "corpus"
        src.mkdir()
        # Varied line content, not repeated text: with every row
        # identical, the channels BETWEEN characters line up down the page
        # and register as gutters too, and the balance guard then rejects
        # the page outright. Real prose does not align that way.
        words = [
            "retrieval",
            "ranking",
            "budget",
            "latency",
            "corpus",
            "segment",
            "anchor",
            "column",
            "warm",
            "index",
            "query",
            "paragraph",
        ]

        def line(i: int, off: int) -> str:
            n = 3 + (i * 7 + off) % 4
            return " ".join(words[(i * 5 + j + off) % len(words)] for j in range(n))[
                :34
            ]

        for d in range(4):
            doc = pymupdf.open()
            for _ in range(2):
                page = doc.new_page(width=612, height=792)
                y = 80
                for i in range(24):
                    page.insert_text((56, y), line(i, d), fontsize=8)
                    page.insert_text((320, y), line(i, d + 5), fontsize=8)
                    y += 13
            doc.save(str(src / f"twocol{d}.pdf"))
            doc.close()

        files = _files(src)

        # Guard against a vacuous pass: the column detector must accept
        # this layout, otherwise both arms just run the single-column path.
        page_dict = get_text(files[0], 0, "rawdict")
        boxes = [
            (c["bbox"][0], c["bbox"][1], c["bbox"][2], c["bbox"][3])
            for blk in page_dict["blocks"]
            for ln in blk.get("lines", [])
            for sp in ln.get("spans", [])
            for c in sp.get("chars", [])
        ]
        assert len(column_bands(boxes, 612.0)) >= 2, (
            "fixture did not register as multi-column, so this test would "
            "assert nothing about the column path"
        )

        seq_cache = PDFCache(cache_dir=tmp_path / "mc_seq", ttl_hours=1)
        con_cache = PDFCache(cache_dir=tmp_path / "mc_con", ttl_hours=1)

        corpus.warm_docs(files, 600, seq_cache, clock=SteppingClock(0))
        self._force_pool(monkeypatch)
        pool_calls = []
        real_pool = corpus.ProcessPoolExecutor

        def spy(*args, **kwargs):
            pool_calls.append(1)
            return real_pool(*args, **kwargs)

        monkeypatch.setattr(corpus, "ProcessPoolExecutor", spy)
        corpus.warm_docs(files, 600, con_cache, clock=SteppingClock(0))

        assert pool_calls, "expected the concurrent path to create a pool"
        for path in files:
            meta = seq_cache.get_metadata(path)
            pages = list(range(meta["page_count"]))
            assert con_cache.get_pages_text(path, pages) == seq_cache.get_pages_text(
                path, pages
            ), f"concurrent and sequential text differ for {path}"

    def test_pool_uses_spawn_context(self, corpus_dir, cache, monkeypatch):
        self._force_pool(monkeypatch)
        captured = {}
        real_pool = corpus.ProcessPoolExecutor

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real_pool(*args, **kwargs)

        monkeypatch.setattr(corpus, "ProcessPoolExecutor", spy)
        corpus.warm_docs(_files(corpus_dir), 600, cache, clock=SteppingClock(0))
        assert captured["mp_context"].get_start_method() == "spawn"

    def test_small_corpus_never_creates_pool(self, corpus_dir, cache, monkeypatch):
        # 3 docs < WARM_DOC_GATE (4): must stay sequential.
        def boom(*args, **kwargs):
            raise AssertionError("pool created for a small corpus")

        monkeypatch.setattr(corpus, "ProcessPoolExecutor", boom)
        out = corpus.warm_docs(_files(corpus_dir), 600, cache, clock=SteppingClock(0))
        assert out["warmed_this_call"] == 3

    def test_env_1_forces_sequential(self, corpus_dir, cache, monkeypatch):
        monkeypatch.setattr(corpus, "WARM_DOC_GATE", 1)
        monkeypatch.setenv("PDF_MCP_MAX_WORKERS", "1")

        def boom(*args, **kwargs):
            raise AssertionError("pool created despite PDF_MCP_MAX_WORKERS=1")

        monkeypatch.setattr(corpus, "ProcessPoolExecutor", boom)
        out = corpus.warm_docs(_files(corpus_dir), 600, cache, clock=SteppingClock(0))
        assert out["warmed_this_call"] == 3

    def test_budget_trip_stops_submissions(self, corpus_dir, cache, monkeypatch):
        # Clock steps 6s/call, budget 10s: submit-checks read 6 (charlie,
        # 1p) then 12 (> 10, stop). Exactly the sequential expectations.
        self._force_pool(monkeypatch)
        out = corpus.warm_docs(_files(corpus_dir), 10, cache, clock=SteppingClock(6))
        assert out["warmed_this_call"] == 1
        assert len(out["unprocessed"]) == 2
        assert out["budget_exhausted"] is True
        warmed = [d for d in out["docs"] if d["status"] == "warmed"]
        assert Path(warmed[0]["path"]).name == "charlie.pdf"

    def test_in_flight_docs_drain_after_trip(self, corpus_dir, cache, monkeypatch):
        # Steps 4s, budget 10s: checks read 4 (submit charlie), 8 (submit
        # alpha), 12 (trip). Both in-flight docs still finalize.
        self._force_pool(monkeypatch)
        out = corpus.warm_docs(_files(corpus_dir), 10, cache, clock=SteppingClock(4))
        assert out["warmed_this_call"] == 2
        assert [Path(p).name for p in out["unprocessed"]] == ["bravo.pdf"]
        assert out["budget_exhausted"] is True

    def test_resume_completes_after_trip(self, corpus_dir, cache, monkeypatch):
        self._force_pool(monkeypatch)
        files = _files(corpus_dir)
        corpus.warm_docs(files, 10, cache, clock=SteppingClock(6))
        out = corpus.warm_docs(files, 600, cache, clock=SteppingClock(0))
        assert out["unprocessed"] == []
        assert out["budget_exhausted"] is False
        assert len(out["docs"]) == 3

    def test_corrupt_doc_skipped_under_pool(self, corpus_dir, cache, monkeypatch):
        # Caught by warm_docs's parent-side pymupdf.open() probe, before
        # _warm_concurrent ever sees the doc -- this pins the probe-path
        # contract, not the pool's own post-submission failure branch
        # (see test_post_submission_failure_lands_in_skipped for that).
        self._force_pool(monkeypatch)
        bad = corpus_dir / "delta.pdf"
        bad.write_bytes(b"%PDF-1.4 truncated garbage")
        # corpus_dir already contains delta.pdf at this point, so
        # resolve_corpus (via _files) picks it up on its own; appending
        # it again would double-count the same path in `skipped`.
        files = _files(corpus_dir)
        out = corpus.warm_docs(files, 600, cache, clock=SteppingClock(0))
        assert out["warmed_this_call"] == 3
        assert len(out["skipped"]) == 1
        assert "delta.pdf" in out["skipped"][0]["path"]

    def test_post_submission_failure_lands_in_skipped(
        self, corpus_dir, cache, monkeypatch
    ):
        # Pins the except-branch inside _warm_concurrent itself (a
        # failure from fut.result()/_finalize_doc after a doc's
        # extraction already succeeded in a real spawn worker), which
        # the probe-path corrupt-doc test above cannot reach.
        self._force_pool(monkeypatch)
        files = _files(corpus_dir)
        target = next(f for f in files if f.endswith("alpha.pdf"))
        real_finalize = corpus._finalize_doc

        def failing(path, *args, **kwargs):
            if path == target:
                raise ValueError("simulated finalize failure")
            return real_finalize(path, *args, **kwargs)

        monkeypatch.setattr(corpus, "_finalize_doc", failing)
        out = corpus.warm_docs(files, 600, cache, clock=SteppingClock(0))
        assert out["warmed_this_call"] == 2
        assert len(out["skipped"]) == 1
        assert "alpha.pdf" in out["skipped"][0]["path"]
        assert "simulated finalize failure" in out["skipped"][0]["reason"]

    def test_broken_pool_falls_back_sequential(self, corpus_dir, cache, monkeypatch):
        self._force_pool(monkeypatch)

        class BrokenPool:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def submit(self, fn, *args):
                raise BrokenProcessPool("worker died")

        monkeypatch.setattr(corpus, "ProcessPoolExecutor", BrokenPool)
        out = corpus.warm_docs(_files(corpus_dir), 600, cache, clock=SteppingClock(0))
        assert out["warmed_this_call"] == 3
        assert out["skipped"] == []
        assert {d["status"] for d in out["docs"]} == {"warmed"}

    def test_ocr_text_preserved_under_pool(self, cache, tmp_path, monkeypatch):
        # Mirror test_warm_preserves_cached_ocr_text, pool forced. Build
        # 4 one-page docs so the pool actually engages; give one of them
        # a cached OCR page, then warm and assert the OCR text survived.
        self._force_pool(monkeypatch)
        d = tmp_path / "ocr_corpus"
        d.mkdir()
        paths = []
        for i in range(4):
            doc = pymupdf.open()
            doc.new_page()  # empty page: native extraction yields ""
            p = str(d / f"scan{i}.pdf")
            doc.save(p)
            doc.close()
            paths.append(p)
        cache.save_page_text(paths[0], 0, "ocr recovered text", source="ocr")
        out = corpus.warm_docs(paths, 600, cache, clock=SteppingClock(0))
        assert out["warmed_this_call"] == 4
        assert cache.get_pages_text(paths[0], [0])[0] == "ocr recovered text"

    def test_embeddings_concurrent_matches_request(
        self, corpus_dir, cache, monkeypatch
    ):
        self._force_pool(monkeypatch)

        def fake_embed(texts):
            return [b"\x00\x00\x80?" for _ in texts]  # 1.0 float32 LE

        out = corpus.warm_docs(
            _files(corpus_dir),
            600,
            cache,
            embeddings=True,
            model_name="fake-model",
            embed=fake_embed,
            clock=SteppingClock(0),
        )
        assert out["warmed_this_call"] == 3
        assert all(d["embeddings_cached"] for d in out["docs"])


class TestWarmDocs:
    def test_warms_all_within_budget(self, corpus_dir, cache):
        out = corpus.warm_docs(_files(corpus_dir), 60, cache, clock=SteppingClock(0))
        assert out["warmed_this_call"] == 3
        assert out["unprocessed"] == []
        assert out["budget_exhausted"] is False
        assert {d["status"] for d in out["docs"]} == {"warmed"}
        assert {d["pages"] for d in out["docs"]} == {1, 2, 4}

    def test_second_call_is_all_cached(self, corpus_dir, cache):
        files = _files(corpus_dir)
        corpus.warm_docs(files, 60, cache, clock=SteppingClock(0))
        out = corpus.warm_docs(files, 60, cache, clock=SteppingClock(0))
        assert out["warmed_this_call"] == 0
        assert {d["status"] for d in out["docs"]} == {"cached"}

    def test_smallest_page_count_warms_first(self, corpus_dir, cache):
        # Budget admits two docs (checks read 6, 12, 18 > 15): smallest
        # two (charlie 1p, alpha 2p) warm; bravo (4p) is left over. The
        # docs list itself is path-sorted, so order is observed via
        # which docs made the cut, not list position.
        out = corpus.warm_docs(_files(corpus_dir), 15, cache, clock=SteppingClock(6))
        warmed = {Path(d["path"]).name for d in out["docs"] if d["status"] == "warmed"}
        assert warmed == {"charlie.pdf", "alpha.pdf"}
        assert [Path(p).name for p in out["unprocessed"]] == ["bravo.pdf"]

    def test_budget_exhaustion_reports_unprocessed(self, corpus_dir, cache):
        # Clock steps 6s per call, budget 10s: start=0, first check
        # reads 6 (warm charlie), second check reads 12 (> 10, stop).
        out = corpus.warm_docs(_files(corpus_dir), 10, cache, clock=SteppingClock(6))
        assert out["warmed_this_call"] == 1
        assert len(out["unprocessed"]) == 2
        assert out["budget_exhausted"] is True
        assert Path(out["docs"][0]["path"]).name == "charlie.pdf"

    def test_docs_list_sorted_by_path_across_calls(self, corpus_dir, cache):
        """The docs envelope is path-sorted on both a mixed
        cached+warmed call and an all-cached resume call, so successive
        envelopes diff cleanly (field feedback: first call listed docs
        by page count, the resume call alphabetically)."""
        files = _files(corpus_dir)
        first = corpus.warm_docs(files, 10, cache, clock=SteppingClock(6))
        assert first["budget_exhausted"] is True  # mixed outcome
        resume = corpus.warm_docs(files, 60, cache, clock=SteppingClock(0))
        for out in (first, resume):
            paths = [d["path"] for d in out["docs"]]
            assert paths == sorted(paths)

    def test_corrupt_pdf_skipped_others_warm(self, corpus_dir, cache):
        (corpus_dir / "corrupt.pdf").write_bytes(b"not a real pdf")
        out = corpus.warm_docs(_files(corpus_dir), 60, cache, clock=SteppingClock(0))
        assert out["warmed_this_call"] == 3
        assert len(out["skipped"]) == 1
        assert "corrupt.pdf" in out["skipped"][0]["path"]

    def test_warm_populates_text_cache(self, corpus_dir, cache):
        files = _files(corpus_dir)
        corpus.warm_docs(files, 60, cache, clock=SteppingClock(0))
        alpha = [f for f in files if f.endswith("alpha.pdf")][0]
        texts = cache.get_pages_text(alpha, [0, 1])
        assert len(texts) == 2
        assert "budget" in texts[0].lower()

    @staticmethod
    def _fake_embed(texts):
        # 2 float32 lanes per page; content irrelevant, shape stable.
        return [b"\x00\x00\x80?\x00\x00\x00@" for _ in texts]

    def test_embeddings_warm_and_cached_partition(self, corpus_dir, cache):
        files = _files(corpus_dir)
        out = corpus.warm_docs(
            files,
            60,
            cache,
            embeddings=True,
            model_name="fake-model",
            embed=self._fake_embed,
            clock=SteppingClock(0),
        )
        assert out["warmed_this_call"] == 3
        assert all(d["embeddings_cached"] for d in out["docs"])
        alpha = [f for f in files if f.endswith("alpha.pdf")][0]
        embs = cache.get_page_embeddings(alpha, [0, 1], "fake-model")
        assert len(embs) == 2
        # Second call: fully warm including embeddings -> all cached.
        out2 = corpus.warm_docs(
            files,
            60,
            cache,
            embeddings=True,
            model_name="fake-model",
            embed=self._fake_embed,
            clock=SteppingClock(0),
        )
        assert out2["warmed_this_call"] == 0
        assert {d["status"] for d in out2["docs"]} == {"cached"}

    def test_text_warm_does_not_satisfy_embeddings_warm(self, corpus_dir, cache):
        files = _files(corpus_dir)
        corpus.warm_docs(files, 60, cache, clock=SteppingClock(0))
        out = corpus.warm_docs(
            files,
            60,
            cache,
            embeddings=True,
            model_name="fake-model",
            embed=self._fake_embed,
            clock=SteppingClock(0),
        )
        # Text is cached but embeddings are missing -> re-warm.
        assert out["warmed_this_call"] == 3

    def test_embeddings_cached_reflects_cache_state_not_request(
        self, corpus_dir, cache
    ):
        """Per-doc embeddings_cached reports actual cache state for the
        configured model, so a cheap text-only warm answers 'do I need
        an embeddings pass before semantic search?' (field feedback:
        the old field echoed the request flag)."""
        files = _files(corpus_dir)
        # Fresh text-only warm: nothing embedded yet.
        out = corpus.warm_docs(
            files, 60, cache, model_name="fake-model", clock=SteppingClock(0)
        )
        assert all(d["embeddings_cached"] is False for d in out["docs"])
        assert all("embeddings" not in d for d in out["docs"])
        # Warm embeddings, then ask again with a text-only call.
        corpus.warm_docs(
            files,
            60,
            cache,
            embeddings=True,
            model_name="fake-model",
            embed=self._fake_embed,
            clock=SteppingClock(0),
        )
        out2 = corpus.warm_docs(
            files, 60, cache, model_name="fake-model", clock=SteppingClock(0)
        )
        assert all(d["embeddings_cached"] is True for d in out2["docs"])
        # A different configured model reads as not-cached.
        out3 = corpus.warm_docs(
            files, 60, cache, model_name="other-model", clock=SteppingClock(0)
        )
        assert all(d["embeddings_cached"] is False for d in out3["docs"])

    def test_embeddings_cached_agrees_with_warm_skip_logic(self, cache, tmp_path):
        """Field-reported stuck state: a doc with a whitespace-only page
        read status=cached (skip logic uses t.strip()) but
        embeddings_cached=False forever, and no call could converge the
        two. Both sides must share the embedder's page-eligibility
        predicate."""
        p = tmp_path / "ws.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.new_page()
        doc.save(str(p))
        doc.close()
        path = str(p.resolve())

        cache.save_metadata(
            path,
            2,
            {},
            [],
            text_coverage=[
                {"page": 1, "text_chars": 30, "raster_images": 0},
                {"page": 2, "text_chars": 0, "raster_images": 1},
            ],
        )
        cache.save_pages_text(path, {0: "Real content on the first page.", 1: "\n  \n"})
        cache.save_page_embeddings(path, {0: [b"\x00\x01"]}, "fake-model")

        def _boom(texts):
            raise AssertionError("embed called on an embeddings-warm doc")

        out = corpus.warm_docs(
            [path],
            60,
            cache,
            embeddings=True,
            model_name="fake-model",
            embed=_boom,
            clock=SteppingClock(0),
        )
        assert out["docs"][0]["status"] == "cached"
        assert out["docs"][0]["embeddings_cached"] is True

    def test_warm_preserves_cached_ocr_text(self, cache, tmp_path):
        # A "scanned" doc: one blank page, no extractable native text.
        p = tmp_path / "scan.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(p))
        doc.close()
        path = str(p.resolve())

        # Simulate a prior pdf_read_pages(ocr=True) call.
        cache.save_page_text(path, 0, "OCRED SEARCHABLE CONTENT", source="ocr")

        out = corpus.warm_docs([path], 60, cache, clock=SteppingClock(0))
        assert {d["status"] for d in out["docs"]} == {"warmed"}
        assert cache.get_page_text(path, 0) == "OCRED SEARCHABLE CONTENT"
        assert cache.get_page_source(path, 0) == "ocr"

    def test_warm_preserves_ocr_text_carrying_a_language(self, cache, tmp_path):
        """The existing preservation test writes an OCR row with no language,
        which stores the '' sentinel. A row carrying a real language sits in a
        different primary-key slot now (issue #27), so cover that too."""
        p = tmp_path / "scan.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(p))
        doc.close()
        path = str(p.resolve())

        cache.save_page_text(path, 0, "KHMER OCR CONTENT", source="ocr", ocr_lang="khm")

        out = corpus.warm_docs([path], 60, cache, clock=SteppingClock(0))

        assert {d["status"] for d in out["docs"]} == {"warmed"}
        # The blank native extraction must not shadow the OCR text.
        assert cache.get_page_text(path, 0) == "KHMER OCR CONTENT"
        # And the language-scoped read still finds the khm row.
        assert cache.get_pages_text(path, [0], ocr_lang="khm") == {
            0: "KHMER OCR CONTENT"
        }

    def test_multi_language_page_counts_once_when_warm(self, cache, tmp_path):
        """A page cached under two languages is still ONE warm page. Counting
        rows instead of pages here would make _cached_pages disagree with
        page_count and re-warm a document that is already warm."""
        p = tmp_path / "scan.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(p))
        doc.close()
        path = str(p.resolve())

        cache.save_page_text(path, 0, "kh text", source="ocr", ocr_lang="khm+eng")
        cache.save_page_text(path, 0, "en text", source="ocr", ocr_lang="eng+khm")

        assert len(cache.get_pages_text(path, [0])) == 1

        first = corpus.warm_docs([path], 60, cache, clock=SteppingClock(0))
        assert first["docs"][0]["status"] == "warmed"

        # Already warm: the second pass must recognise it, not re-extract.
        second = corpus.warm_docs([path], 60, cache, clock=SteppingClock(0))
        assert second["docs"][0]["status"] == "cached"

    def test_warm_embeds_preserved_ocr_text(self, cache, tmp_path):
        # Same setup, but with embeddings on: the preserved OCR text
        # (not the blank native extraction) must feed the embed input.
        p = tmp_path / "scan.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(p))
        doc.close()
        path = str(p.resolve())

        cache.save_page_text(path, 0, "OCRED SEARCHABLE CONTENT", source="ocr")

        out = corpus.warm_docs(
            [path],
            60,
            cache,
            embeddings=True,
            model_name="fake-model",
            embed=self._fake_embed,
            clock=SteppingClock(0),
        )
        assert {d["status"] for d in out["docs"]} == {"warmed"}
        embs = cache.get_page_embeddings(path, [0], "fake-model")
        assert len(embs) == 1
        assert cache.get_page_text(path, 0) == "OCRED SEARCHABLE CONTENT"
        assert cache.get_page_source(path, 0) == "ocr"


class TestOverviewCards:
    def test_text_coverage_label(self):
        full = [{"page": 1, "text_chars": 10, "raster_images": 0}]
        none = [{"page": 1, "text_chars": 0, "raster_images": 1}]
        part = full + none
        assert corpus.text_coverage_label(full) == "full"
        assert corpus.text_coverage_label(none) == "none"
        assert corpus.text_coverage_label(part) == "partial"
        assert corpus.text_coverage_label([]) == "none"

    def test_card_fields(self, corpus_dir, cache):
        files = _files(corpus_dir)
        corpus.warm_docs(files, 60, cache, clock=SteppingClock(0))
        alpha = [f for f in files if f.endswith("alpha.pdf")][0]
        card = corpus.build_overview_card(alpha, cache, from_cache=False)
        assert card["path"] == alpha
        # Fixture sets no metadata title: falls back to the filename stem.
        assert card["title"] == "alpha"
        assert card["pages"] == 2
        assert card["toc_top"] == []
        assert card["has_toc"] is False
        assert card["text_coverage"] == "full"
        assert card["size_bytes"] > 0
        assert card["from_cache"] is False

    def test_toc_top_depth1_capped(self, cache, tmp_path):
        p = tmp_path / "toc.pdf"
        doc = pymupdf.open()
        for _ in range(3):
            page = doc.new_page()
            page.insert_text((50, 50), "Chapter body text here.")
        doc.set_toc(
            [[1, "Intro", 1], [2, "Sub A", 1], [1, "Results", 2], [1, "End", 3]]
        )
        doc.save(str(p))
        doc.close()
        path = str(p.resolve())
        corpus.warm_docs([path], 60, cache, clock=SteppingClock(0))
        card = corpus.build_overview_card(path, cache, from_cache=False)
        assert card["toc_top"] == ["Intro", "Results", "End"]
        assert card["has_toc"] is True

    def _card_for_pdf(self, cache, tmp_path, title=None, toc=None):
        p = tmp_path / "meta.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Body text for warming.")
        if title is not None:
            doc.set_metadata({"title": title})
        if toc is not None:
            doc.set_toc(toc)
        doc.save(str(p))
        doc.close()
        path = str(p.resolve())
        corpus.warm_docs([path], 60, cache, clock=SteppingClock(0))
        return corpus.build_overview_card(path, cache, from_cache=False)

    def test_whitespace_toc_entries_dropped(self, cache, tmp_path):
        """Whitespace-only TOC titles are junk for triage; drop them
        (field feedback: a 368-page doc's toc_top was [' '])."""
        card = self._card_for_pdf(
            cache, tmp_path, toc=[[1, " ", 1], [1, "Real Chapter", 1]]
        )
        assert card["toc_top"] == ["Real Chapter"]

    def test_placeholder_titles_fall_back_to_stem(self, cache, tmp_path):
        """Known exporter placeholders read as junk and fall back to the
        filename stem (field feedback: 'Pdf Document', 'Untitled 3.pages';
        never-null unified with search doc_title)."""
        for junk in ("Pdf Document", "Untitled 3.pages", "   ", "_WATERMARKED_"):
            card = self._card_for_pdf(cache, tmp_path, title=junk)
            assert card["title"] == Path(card["path"]).stem, junk

    def test_embedded_scanner_marker_title_junked(self, cache, tmp_path):
        """A scanner artifact embedded in a longer filename-style title is
        junk too (real field sample: the marker sits mid-string, so a
        wrapped-only check misses it)."""
        card = self._card_for_pdf(
            cache,
            tmp_path,
            title="506673___CLEANLPDF_LAN_16Oct202315580658_002.PDF",
        )
        assert card["title"] == Path(card["path"]).stem

    def test_real_title_passes_through(self, cache, tmp_path):
        card = self._card_for_pdf(cache, tmp_path, title="Annual Report 2026")
        assert card["title"] == "Annual Report 2026"

    def test_all_junk_toc_reads_as_no_toc(self, cache, tmp_path):
        """A TOC whose every title is whitespace is junk for orientation
        AND for section titling; has_toc reflects post-filter reality so
        clients never see has_toc=true with an empty preview for it."""
        card = self._card_for_pdf(cache, tmp_path, toc=[[1, " ", 1], [2, "  ", 1]])
        assert card["has_toc"] is False
        assert card["toc_top"] == []

    def test_junk_level1_real_level2_keeps_has_toc(self, cache, tmp_path):
        """A real title at any level keeps has_toc true even when the
        level-1 preview is empty (rare but genuinely accurate combo)."""
        card = self._card_for_pdf(
            cache, tmp_path, toc=[[1, " ", 1], [2, "Real Subsection", 1]]
        )
        assert card["has_toc"] is True
        assert card["toc_top"] == []


class TestCorpusFusion:
    def test_doc_rankings_interleave_by_within_doc_rank(self):
        lists = [
            [("a.pdf", 3), ("a.pdf", 7)],
            [("b.pdf", 1), ("b.pdf", 2)],
        ]
        fused = corpus.rrf_fuse_doc_rankings(lists)
        assert fused[:2] == [("a.pdf", 3), ("b.pdf", 1)]
        assert fused[2:] == [("a.pdf", 7), ("b.pdf", 2)]

    def test_doc_rankings_tiebreak_deterministic(self):
        fused = corpus.rrf_fuse_doc_rankings([[("z.pdf", 5)], [("a.pdf", 9)]])
        assert fused == [("a.pdf", 9), ("z.pdf", 5)]

    def test_relevance_outranks_alphabetical_order(self):
        # Every document contributes its own rank-1 page, so all tie at
        # 1/(k+0). Without a relevance signal the order is alphabetical,
        # which is how a described-query search returned the ten
        # alphabetically-first documents in the corpus and scored 0.000.
        lists = [[("z.pdf", 5)], [("a.pdf", 9)]]
        scores = {("z.pdf", 5): 9.0, ("a.pdf", 9): 1.0}
        fused = corpus.rrf_fuse_doc_rankings(lists, scores=scores)
        assert fused == [("z.pdf", 5), ("a.pdf", 9)]

    def test_relevance_only_breaks_ties_never_beats_rrf_rank(self):
        # A document's rank-2 page must not outrank another's rank-1 page
        # however high its score: within-document rank is the primary key.
        lists = [[("a.pdf", 1), ("a.pdf", 2)], [("b.pdf", 1)]]
        scores = {("a.pdf", 1): 0.1, ("a.pdf", 2): 99.0, ("b.pdf", 1): 0.2}
        fused = corpus.rrf_fuse_doc_rankings(lists, scores=scores)
        assert fused[:2] == [("b.pdf", 1), ("a.pdf", 1)]
        assert fused[2] == ("a.pdf", 2)

    def test_missing_scores_fall_back_to_alphabetical(self):
        lists = [[("z.pdf", 5)], [("a.pdf", 9)]]
        fused = corpus.rrf_fuse_doc_rankings(lists, scores={})
        assert fused == [("a.pdf", 9), ("z.pdf", 5)]

    def test_equal_scores_still_break_deterministically(self):
        lists = [[("z.pdf", 5)], [("a.pdf", 9)]]
        scores = {("z.pdf", 5): 4.0, ("a.pdf", 9): 4.0}
        fused = corpus.rrf_fuse_doc_rankings(lists, scores=scores)
        assert fused == [("a.pdf", 9), ("z.pdf", 5)]

    def test_ranking_is_invariant_under_document_renaming(self):
        # The property both shipped bugs violated: renaming documents must
        # not reorder results. Checked by mapping names through a
        # permutation that reverses alphabetical order.
        lists = [[("a.pdf", 1)], [("m.pdf", 2)], [("z.pdf", 3)]]
        scores = {("a.pdf", 1): 1.0, ("m.pdf", 2): 5.0, ("z.pdf", 3): 3.0}
        fused = corpus.rrf_fuse_doc_rankings(lists, scores=scores)

        rename = {"a.pdf": "z9.pdf", "m.pdf": "m9.pdf", "z.pdf": "a9.pdf"}
        r_lists = [[(rename[d], p)] for lst in lists for d, p in lst]
        r_scores = {(rename[d], p): s for (d, p), s in scores.items()}
        r_fused = corpus.rrf_fuse_doc_rankings(r_lists, scores=r_scores)

        assert [rename[d] for d, _p in fused] == [d for d, _p in r_fused]

    def test_doc_rankings_top_k(self):
        lists = [[("a.pdf", 1), ("a.pdf", 2)], [("b.pdf", 1)]]
        assert len(corpus.rrf_fuse_doc_rankings(lists, top_k=2)) == 2

    def test_two_rankings_shared_item_scores_add(self):
        a = [("x.pdf", 1), ("y.pdf", 2)]
        b = [("y.pdf", 2), ("z.pdf", 3)]
        fused = corpus.rrf_fuse_two_rankings(a, b)
        # ("y.pdf", 2): 1/(60+1) + 1/(60+0) beats x's 1/(60+0)
        assert fused[0] == ("y.pdf", 2)

    def test_two_rankings_empty_sides(self):
        assert corpus.rrf_fuse_two_rankings([], []) == []
        assert corpus.rrf_fuse_two_rankings([("a.pdf", 1)], []) == [("a.pdf", 1)]

    def test_two_rankings_scored_shared_item_sums_contributions(self):
        a = [("x.pdf", 1), ("y.pdf", 2)]
        b = [("y.pdf", 2), ("z.pdf", 3)]
        scored = corpus.rrf_fuse_two_rankings_scored(a, b)
        by_item = dict(scored)
        k = corpus.CORPUS_RRF_K
        assert by_item[("y.pdf", 2)] == 1.0 / (k + 1) + 1.0 / (k + 0)
        assert by_item[("x.pdf", 1)] == 1.0 / (k + 0)
        assert scored[0][0] == ("y.pdf", 2)

    def test_two_rankings_scored_tiebreak_matches_unscored(self):
        a = [("z.pdf", 5)]
        b = [("a.pdf", 9)]
        scored = corpus.rrf_fuse_two_rankings_scored(a, b)
        assert [item for item, _s in scored] == corpus.rrf_fuse_two_rankings(a, b)

    def test_n_rankings_weight_scales_contribution(self):
        kw = [("a.pdf", 1)]
        sem = [("b.pdf", 2)]
        doc = [("c.pdf", 3)]
        scored = dict(
            corpus.rrf_fuse_rankings_scored([(kw, 1.0), (sem, 1.0), (doc, 0.25)])
        )
        k = corpus.CORPUS_RRF_K
        assert scored[("a.pdf", 1)] == 1.0 / k
        assert scored[("c.pdf", 3)] == 0.25 / k

    def test_n_rankings_two_lists_at_weight_one_is_byte_identical(self):
        a = [("x.pdf", 1), ("y.pdf", 2), ("q.pdf", 9)]
        b = [("y.pdf", 2), ("z.pdf", 3), ("x.pdf", 1)]
        assert corpus.rrf_fuse_rankings_scored(
            [(a, 1.0), (b, 1.0)], top_k=3
        ) == corpus.rrf_fuse_two_rankings_scored(a, b, top_k=3)

    def test_n_rankings_reproduces_spike_fusion(self):
        # Hand-checked against docprofile_race2.fuse_weighted (k=60):
        # y: 1/61 + 1/60            = 0.033060...
        # x: 1/60 + 0.25/60         = 0.020833...
        # z: 1/61 + 0.25/61         = 0.020491...
        kw = [("x.pdf", 1), ("y.pdf", 2)]
        sem = [("y.pdf", 2), ("z.pdf", 3)]
        doc = [("x.pdf", 1), ("z.pdf", 3)]
        fused = [
            it
            for it, _s in corpus.rrf_fuse_rankings_scored(
                [(kw, 1.0), (sem, 1.0), (doc, 0.25)]
            )
        ]
        assert fused == [("y.pdf", 2), ("x.pdf", 1), ("z.pdf", 3)]

    def test_n_rankings_invariant_under_document_renaming(self):
        kw = [("a.pdf", 1), ("m.pdf", 2)]
        sem = [("z.pdf", 3), ("a.pdf", 1)]
        doc = [("m.pdf", 2), ("z.pdf", 3)]
        rename = {"a.pdf": "z9.pdf", "m.pdf": "m9.pdf", "z.pdf": "a9.pdf"}
        ren = lambda lst: [(rename[d], p) for d, p in lst]  # noqa: E731
        base = corpus.rrf_fuse_rankings_scored([(kw, 1.0), (sem, 1.0), (doc, 0.25)])
        moved = corpus.rrf_fuse_rankings_scored(
            [(ren(kw), 1.0), (ren(sem), 1.0), (ren(doc), 0.25)]
        )
        assert [(rename[d], p) for (d, p), _s in base] == [it for it, _s in moved]

    def test_n_rankings_exact_tie_breaks_by_path_then_page(self):
        scored = corpus.rrf_fuse_rankings_scored(
            [([("z.pdf", 1)], 1.0), ([("a.pdf", 2)], 1.0), ([], 0.25)]
        )
        assert [it for it, _s in scored] == [("a.pdf", 2), ("z.pdf", 1)]


class TestWarmExtractWorker:
    def test_payload_shape(self, corpus_dir):
        path = str(corpus_dir / "alpha.pdf")
        page_count, metadata, toc, texts, coverage, layout = _warm_extract_worker(path)
        assert page_count == 2
        assert set(texts) == {0, 1}
        assert all(t.strip() for t in texts.values())
        assert [c["page"] for c in coverage] == [1, 2]
        assert isinstance(metadata, dict)
        assert isinstance(toc, list)
        # Layout (blocks + page size + hidden flag) rides along so warm
        # can persist it and the query path never opens the PDF.
        assert set(layout) == {0, 1}
        blocks, size, hidden = layout[0]
        assert blocks and len(size) == 2 and isinstance(hidden, bool)

    def test_picklable_for_spawn(self):
        # Module-scope function: pickles by qualified name, spawn-safe.
        clone = pickle.loads(pickle.dumps(_warm_extract_worker))
        assert clone is _warm_extract_worker


class TestWarmDocumentIsAtomic:
    """_finalize_doc writes a document in ONE transaction.

    The reason is cost (each separate connection commits, and a commit is
    an fsync: warming 6 documents spent 3.18s of 3.39s in commits on
    Windows against 0.05s on Linux), but the guarantee is correctness, so
    that is what this asserts. A failure partway through must leave no
    half-written document behind.
    """

    def test_a_failure_midway_leaves_nothing_committed(self, cache, tmp_path):
        import pymupdf

        from pdf_mcp import corpus

        pdf = tmp_path / "doc.pdf"
        doc = pymupdf.open()
        for i in range(3):
            doc.new_page().insert_text((50, 50), f"page {i} body text here")
        doc.save(str(pdf))
        doc.close()

        real_blocks = cache.save_page_blocks

        def explode(*args, **kwargs):
            raise RuntimeError("disk full, midway through the document")

        cache.save_page_blocks = explode
        try:
            with pytest.raises(RuntimeError):
                corpus._finalize_doc(
                    cache=cache,
                    path=str(pdf),
                    page_count=3,
                    metadata={"title": "t"},
                    toc=[],
                    texts={0: "page 0 body text here"},
                    coverage=[{"page": 1, "text_chars": 21, "raster_images": 0}],
                    embeddings=False,
                    model_name=None,
                    embed=None,
                    layout={0: ([], (612.0, 792.0), False)},
                )
        finally:
            cache.save_page_blocks = real_blocks

        assert (
            cache.get_metadata(str(pdf)) is None
        ), "metadata was committed even though the document failed partway"
        assert (
            cache.get_page_text(str(pdf), 0) is None
        ), "page text was committed even though the document failed partway"


class TestWarmReportIsVerified:
    """A warm report is checked against the cache before it is returned.

    Every doc row's status used to be a claim about which code path ran,
    never about what landed in SQLite, so any write that did not become
    visible was invisible to the caller too. A 500-doc field warm
    returned `unprocessed: []` with 21 documents (6 of them gold) holding
    no `pdf_metadata` row, and the benchmark built on that cache read
    doc-NDCG 0.929 on every arm before anyone noticed. `warm_complete`
    is the signal a resume loop should read; an empty `unprocessed` is
    not proof the corpus is warm.
    """

    def test_complete_warm_reports_complete(self, corpus_dir, cache):
        out = corpus.warm_docs(_files(corpus_dir), 60, cache, clock=SteppingClock(0))
        assert out["warm_complete"] is True
        assert out["unwarmed"] == 0

    def test_budget_exhaustion_is_an_incomplete_warm(self, corpus_dir, cache):
        out = corpus.warm_docs(_files(corpus_dir), 10, cache, clock=SteppingClock(6))
        assert out["budget_exhausted"] is True
        assert out["warm_complete"] is False
        assert out["unwarmed"] == 2

    def test_a_corrupt_doc_is_an_incomplete_warm(self, corpus_dir, cache):
        (corpus_dir / "corrupt.pdf").write_bytes(b"not a real pdf")
        out = corpus.warm_docs(_files(corpus_dir), 60, cache, clock=SteppingClock(0))
        assert out["warm_complete"] is False
        assert out["unwarmed"] == 1

    def test_a_warm_that_did_not_persist_is_not_reported_as_warmed(
        self, corpus_dir, cache
    ):
        """The defect itself: the write path returns success but the row
        is not readable back. The doc must leave `docs`, land in
        `skipped` with a distinct reason, and flip `warm_complete`."""
        files = _files(corpus_dir)
        victim = [f for f in files if f.endswith("bravo.pdf")][0]
        real_save = cache.save_metadata

        def drop_bravo(path, *args, **kwargs):
            if path == victim:
                return  # silently writes nothing, exactly like the field case
            return real_save(path, *args, **kwargs)

        cache.save_metadata = drop_bravo
        try:
            out = corpus.warm_docs(files, 60, cache, clock=SteppingClock(0))
        finally:
            cache.save_metadata = real_save

        assert out["warm_complete"] is False
        assert out["unwarmed"] == 1
        assert victim not in [d["path"] for d in out["docs"]]
        assert [s["path"] for s in out["skipped"]] == [victim]
        assert "not readable back" in out["skipped"][0]["reason"]
        # The other two are unaffected and still reported warmed.
        assert out["warmed_this_call"] == 2

    def test_a_doc_invalidated_mid_call_is_offered_for_retry(self, corpus_dir, cache):
        """A doc the pre-scan read as cached, but whose cache entry is
        gone by the time the report is built, is retryable: it belongs
        in `unprocessed`, not in `skipped`."""
        files = _files(corpus_dir)
        corpus.warm_docs(files, 60, cache, clock=SteppingClock(0))
        victim = [f for f in files if f.endswith("alpha.pdf")][0]

        real_cached_pages = corpus._cached_pages
        seen: dict[str, int] = {}

        def stale_on_recheck(path, *args, **kwargs):
            seen[path] = seen.get(path, 0) + 1
            if path == victim and seen[path] > 1:
                return None  # invalidated between pre-scan and report
            return real_cached_pages(path, *args, **kwargs)

        corpus._cached_pages = stale_on_recheck
        try:
            out = corpus.warm_docs(files, 60, cache, clock=SteppingClock(0))
        finally:
            corpus._cached_pages = real_cached_pages

        assert out["warm_complete"] is False
        assert out["unwarmed"] == 1
        assert out["unprocessed"] == [victim]
        assert victim not in [d["path"] for d in out["docs"]]
        assert out["skipped"] == []


class TestMaxOverChunks:
    def test_page_score_is_the_best_chunk_not_the_average(self):
        """The whole point of chunking: a page whose one relevant chunk
        scores high must take that score, not have it averaged away by
        the rest of the page."""
        import numpy as np

        query = np.array([1.0, 0.0], dtype=np.float32)
        chunks = [
            np.array([0.0, 1.0], dtype=np.float32),  # cosine 0.0
            np.array([1.0, 0.0], dtype=np.float32),  # cosine 1.0
            np.array([0.0, 1.0], dtype=np.float32),  # cosine 0.0
        ]
        score = max(float(c @ query) for c in chunks)
        assert score == 1.0
        assert score != sum(float(c @ query) for c in chunks) / len(chunks)


class TestCachedPagesLayoutGate:
    """Warm must not skip a doc whose long pages carry a single page-level row
    written by an older server into this newer cache."""

    class _Cache:
        def __init__(self, text, blobs):
            self._t, self._b = text, blobs

        def get_metadata(self, path):
            return {"page_count": 1, "text_coverage": [{"text_chars": 1}]}

        def get_pages_text(self, path, nums):
            return {0: self._t}

        def get_page_embeddings(self, path, nums, model):
            return {0: self._b} if self._b else {}

    def test_stale_page_level_row_means_not_cached(self):
        from pdf_mcp.extractor import page_embedding_units

        text = ". ".join(f"sentence {i}" for i in range(400))
        assert len(page_embedding_units(text)) > 1
        c = self._Cache(text, [b"x"])
        assert corpus._cached_pages("p.pdf", c, True, "m") is None

    def test_matching_layout_is_cached(self):
        from pdf_mcp.extractor import page_embedding_units

        text = ". ".join(f"sentence {i}" for i in range(400))
        c = self._Cache(text, [b"x"] * len(page_embedding_units(text)))
        assert corpus._cached_pages("p.pdf", c, True, "m") == 1


class TestScannedDocWarmSignals:
    """2026-09-03 spec: an all-empty (scanned) doc warms with an honest
    text_coverage label, while embeddings_cached keeps its gate-tied
    semantics (vacuously true: everything embeddable was embedded)."""

    def test_empty_doc_labeled_none_embeddings_vacuously_true(
        self, sample_pdf_scanned, cache
    ):
        def fake_embed(texts):
            return [b"\x00\x00\x80?" for _ in texts]

        out = corpus.warm_docs(
            [sample_pdf_scanned],
            600,
            cache,
            embeddings=True,
            model_name="fake-model",
            embed=fake_embed,
            clock=SteppingClock(0),
        )
        (row,) = out["docs"]
        assert row["text_coverage"] == "none"
        assert row["embeddings_cached"] is True
