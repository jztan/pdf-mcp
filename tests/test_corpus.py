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
