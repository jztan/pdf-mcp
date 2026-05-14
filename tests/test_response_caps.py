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
