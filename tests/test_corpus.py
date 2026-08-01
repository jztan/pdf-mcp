"""Tests for corpus resolution and warm orchestration (corpus.py)."""

import pickle
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pymupdf

from pdf_mcp import corpus
from pdf_mcp.extractor import _warm_extract_worker


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
        cache.save_page_embeddings(path, {0: b"\x00\x01"}, "fake-model")

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


class TestWarmExtractWorker:
    def test_payload_shape(self, corpus_dir):
        path = str(corpus_dir / "alpha.pdf")
        page_count, metadata, toc, texts, coverage = _warm_extract_worker(path)
        assert page_count == 2
        assert set(texts) == {0, 1}
        assert all(t.strip() for t in texts.values())
        assert [c["page"] for c in coverage] == [1, 2]
        assert isinstance(metadata, dict)
        assert isinstance(toc, list)

    def test_picklable_for_spawn(self):
        # Module-scope function: pickles by qualified name, spawn-safe.
        clone = pickle.loads(pickle.dumps(_warm_extract_worker))
        assert clone is _warm_extract_worker
