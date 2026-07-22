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
        out = corpus.warm_docs(_files(corpus_dir), 60, cache, clock=SteppingClock(0))
        order = [Path(d["path"]).name for d in out["docs"] if d["status"] == "warmed"]
        assert order == ["charlie.pdf", "alpha.pdf", "bravo.pdf"]

    def test_budget_exhaustion_reports_unprocessed(self, corpus_dir, cache):
        # Clock steps 6s per call, budget 10s: start=0, first check
        # reads 6 (warm charlie), second check reads 12 (> 10, stop).
        out = corpus.warm_docs(_files(corpus_dir), 10, cache, clock=SteppingClock(6))
        assert out["warmed_this_call"] == 1
        assert len(out["unprocessed"]) == 2
        assert out["budget_exhausted"] is True
        assert Path(out["docs"][0]["path"]).name == "charlie.pdf"

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
        assert all(d["embeddings"] for d in out["docs"])
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
