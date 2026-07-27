"""Tests for corpus resolution and warm orchestration (corpus.py)."""

from pathlib import Path

import pymupdf

from pdf_mcp import corpus


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
        assert card["title"] is None  # fixture sets no metadata title
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

    def test_placeholder_titles_nulled(self, cache, tmp_path):
        """Known exporter placeholders read as no-title (field
        feedback: 'Pdf Document', 'Untitled 3.pages')."""
        for junk in ("Pdf Document", "Untitled 3.pages", "   "):
            card = self._card_for_pdf(cache, tmp_path, title=junk)
            assert card["title"] is None, junk

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
