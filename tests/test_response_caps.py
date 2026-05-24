"""Tests for byte-cap behavior on pdf_read_all and section-search."""

import tempfile
from pathlib import Path

import pymupdf

from pdf_mcp.server import _apply_byte_cap, pdf_read_all


class TestApplyByteCap:
    def test_returns_all_parts_when_under_cap(self):
        text, included, returned, available = _apply_byte_cap(
            ["abc", "def", "ghi"], cap=100
        )
        assert text == "abc\n\ndef\n\nghi"
        assert included == 3
        assert returned == len("abc\n\ndef\n\nghi".encode("utf-8"))
        assert available == returned

    def test_stops_at_part_boundary_when_over_cap(self):
        text, included, returned, available = _apply_byte_cap(
            ["aaaa", "bbbb", "cccc"], cap=6
        )
        # only first part fits: 4 bytes < 6, adding "\n\nbbbb" would push to 10
        assert text == "aaaa"
        assert included == 1
        assert returned == 4
        assert available == len("aaaa\n\nbbbb\n\ncccc".encode("utf-8"))

    def test_zero_parts(self):
        text, included, returned, available = _apply_byte_cap([], cap=100)
        assert text == ""
        assert included == 0
        assert returned == 0
        assert available == 0

    def test_multibyte_utf8_counted_in_bytes(self):
        # "日" is 3 UTF-8 bytes
        text, included, returned, available = _apply_byte_cap(
            ["日日日", "日日日"], cap=10
        )
        # First part: 9 bytes. Adding separator + second: would be 9+2+9=20.
        assert text == "日日日"
        assert included == 1
        assert returned == 9
        assert available == 20


def _make_pdf(pages_text: list[str]) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    doc = pymupdf.open()
    for body in pages_text:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(36, 36, 560, 800), body, fontsize=10)
    doc.save(f.name)
    doc.close()
    f.close()
    return str(Path(f.name).resolve())


class TestPdfReadAllByteCap:
    def test_uncapped_when_under_budget(self, isolated_server):
        path = _make_pdf(["short page one.", "short page two."])
        try:
            result = pdf_read_all(path)
        finally:
            Path(path).unlink(missing_ok=True)
        assert result["truncated"] is False
        assert result["truncated_bytes"] is False
        assert result["truncated_pages"] is False
        assert result["bytes_returned"] == result["bytes_available"]
        assert result["next_page"] is None

    def test_byte_cap_truncates(self, isolated_server, monkeypatch):
        from pdf_mcp import server as server_module

        monkeypatch.setattr(
            server_module.pdf_config,
            "_data",
            {"limits": {"max_response_bytes": 4096}},
            raising=False,
        )
        body = "Lorem ipsum dolor sit amet. " * 80
        path = _make_pdf([body] * 6)
        try:
            result = pdf_read_all(path, max_pages=50)
        finally:
            Path(path).unlink(missing_ok=True)
        assert result["truncated"] is True
        assert result["truncated_bytes"] is True
        assert result["truncated_pages"] is False
        assert result["bytes_returned"] < result["bytes_available"]
        assert isinstance(result["next_page"], int)
        assert result["next_page"] >= 2
        assert result["page_count"] < 6

    def test_page_cap_still_sets_truncated_pages(self, isolated_server):
        path = _make_pdf(["p1", "p2", "p3", "p4", "p5"])
        try:
            result = pdf_read_all(path, max_pages=2)
        finally:
            Path(path).unlink(missing_ok=True)
        assert result["truncated"] is True
        assert result["truncated_pages"] is True
        assert result["truncated_bytes"] is False
        assert result["next_page"] == 3


class TestPdfReadAllStartPage:
    """Tests for the start_page resume parameter on pdf_read_all."""

    def test_start_page_skips_earlier_pages(self, isolated_server):
        path = _make_pdf(
            [
                "alpha page one body.",
                "bravo page two body.",
                "charlie page three body.",
            ]
        )
        try:
            result = pdf_read_all(path, start_page=2)
        finally:
            Path(path).unlink(missing_ok=True)
        assert "alpha" not in result["full_text"]
        assert "bravo" in result["full_text"]
        assert "charlie" in result["full_text"]
        assert result["start_page"] == 2
        assert result["page_count"] == 2

    def test_start_page_below_one_clamps_to_one(self, isolated_server):
        path = _make_pdf(["a", "b", "c"])
        try:
            for bad in (0, -1, -999):
                result = pdf_read_all(path, start_page=bad)
                assert result["start_page"] == 1
                assert result["page_count"] == 3
        finally:
            Path(path).unlink(missing_ok=True)

    def test_start_page_past_end_returns_empty_window(self, isolated_server):
        path = _make_pdf(["a", "b", "c"])
        try:
            result = pdf_read_all(path, start_page=100)
        finally:
            Path(path).unlink(missing_ok=True)
        assert result["page_count"] == 0
        assert result["full_text"] == ""
        assert result["next_page"] is None
        assert result["truncated"] is False
        assert result["bytes_returned"] == 0
        assert result["bytes_available"] == 0

    def test_next_page_after_byte_cap_is_consumable(self, isolated_server, monkeypatch):
        """Property test: resuming with start_page=next_page eventually
        covers every page of the document exactly once, in order. This
        is the invariant the response contract promises — if next_page
        is set, calling the same tool with start_page=next_page must
        actually work and continue from the right place."""
        from pdf_mcp import server as server_module

        monkeypatch.setattr(
            server_module.pdf_config,
            "_data",
            {"limits": {"max_response_bytes": 4096}},
            raising=False,
        )
        body = "Lorem ipsum dolor sit amet. " * 80  # ~2KB each
        path = _make_pdf([f"PAGE{i:02d}_MARKER {body}" for i in range(10)])
        try:
            collected: list[str] = []
            seen_starts: list[int] = []
            cursor: int | None = 1
            iterations = 0
            while cursor is not None and iterations < 20:
                iterations += 1
                seen_starts.append(cursor)
                result = pdf_read_all(path, start_page=cursor, max_pages=50)
                assert result["start_page"] == cursor
                collected.append(result["full_text"])
                cursor = result["next_page"]
        finally:
            Path(path).unlink(missing_ok=True)

        assert iterations >= 2, "byte cap should have forced at least one resume"
        assert cursor is None, "loop terminated without exhausting next_page"
        # No start_page repeated — pagination strictly advances.
        assert len(seen_starts) == len(set(seen_starts))
        # Every page marker appears exactly once across all collected text.
        joined = "\n\n".join(collected)
        for i in range(10):
            marker = f"PAGE{i:02d}_MARKER"
            count = joined.count(marker)
            assert count == 1, f"{marker} appeared {count} times, expected exactly 1"

    def test_next_page_after_page_cap_is_consumable(self, isolated_server):
        """Same invariant under the max_pages cap rather than the byte cap."""
        path = _make_pdf([f"PAGE{i:02d}_BODY" for i in range(8)])
        try:
            r1 = pdf_read_all(path, max_pages=3, start_page=1)
            assert r1["truncated_pages"] is True
            assert r1["next_page"] == 4
            r2 = pdf_read_all(path, max_pages=3, start_page=r1["next_page"])
            assert r2["start_page"] == 4
            assert r2["next_page"] == 7
            r3 = pdf_read_all(path, max_pages=3, start_page=r2["next_page"])
            assert r3["start_page"] == 7
            assert r3["next_page"] is None  # only 2 pages left, fits in 3
        finally:
            Path(path).unlink(missing_ok=True)


class TestSectionSearchByteCap:
    def test_long_title_truncated(self, isolated_server, monkeypatch):
        from pdf_mcp import server as server_module

        long_title = "A" * 5000
        monkeypatch.setattr(
            server_module.cache, "get_section_fts_coverage", lambda _p: 1
        )
        monkeypatch.setattr(
            server_module.cache,
            "search_section_fts",
            lambda _p, _q, _n: [
                {
                    "section_id": 1,
                    "title": long_title,
                    "title_source": "heading_detected",
                    "start_page": 1,
                    "end_page": 1,
                    "score": 0.5,
                }
            ],
        )
        out = server_module._pdf_search_section_mode("/tmp/x.pdf", "q", 10)
        match = out["sections"][0]
        assert match["title_truncated"] is True
        assert len(match["title"].encode("utf-8")) <= 2048

    def test_byte_cap_drops_trailing_matches(self, isolated_server, monkeypatch):
        from pdf_mcp import server as server_module

        monkeypatch.setattr(
            server_module.pdf_config,
            "_data",
            {"limits": {"max_response_bytes": 4096}},
            raising=False,
        )
        matches = [
            {
                "section_id": i,
                "title": f"Section {i} " + ("x" * 80),
                "title_source": "toc",
                "start_page": i,
                "end_page": i,
                "score": 1.0 / (i + 1),
            }
            for i in range(200)
        ]
        monkeypatch.setattr(
            server_module.cache, "get_section_fts_coverage", lambda _p: 200
        )
        monkeypatch.setattr(
            server_module.cache,
            "search_section_fts",
            lambda _p, _q, _n: matches,
        )
        out = server_module._pdf_search_section_mode("/tmp/x.pdf", "q", 200)
        assert out["truncated_bytes"] is True
        assert out["matches_omitted"] > 0
        assert len(out["sections"]) + out["matches_omitted"] == 200
