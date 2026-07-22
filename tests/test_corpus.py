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
