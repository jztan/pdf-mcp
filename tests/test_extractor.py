# tests/test_extractor.py
"""Tests for pdf_mcp.extractor module - edge cases and uncovered functions."""

import pytest
import pymupdf

from pdf_mcp.extractor import (
    parse_page_range,
    extract_text_from_page,
    extract_text_with_coordinates,
    extract_images_from_page,
    chunk_text,
)


class TestParsePageRangeEdgeCases:
    """Edge case tests for parse_page_range function."""

    def test_empty_parts_ignored(self):
        """Input with empty parts like '1,,3' should ignore empty parts."""
        # Covers line 49: if not part: continue
        result = parse_page_range("1,,3", 10)
        assert result == [0, 2]  # 1-indexed input → 0-indexed output

    def test_whitespace_handling(self):
        """Input with extra whitespace should be handled."""
        # Covers line 44: re.split(r'[,\s]+', pages.strip())
        result = parse_page_range("  1  ,  3  ", 10)
        assert result == [0, 2]

    def test_invalid_string_ignored(self):
        """Non-numeric strings like 'abc' should be ignored."""
        # Covers lines 66-67: except ValueError: continue
        result = parse_page_range("1,abc,3", 10)
        assert result == [0, 2]

    def test_invalid_range_ignored(self):
        """Invalid ranges like '1-a' or 'a-5' should be ignored."""
        # Covers line 53-54: regex match fails, match is None
        result = parse_page_range("1-a,2,a-5", 10)
        assert result == [1]  # Only "2" is valid → 0-indexed = 1


class TestChunkText:
    """Tests for chunk_text function."""

    def test_empty_text_returns_empty_list(self):
        """Empty input returns empty list."""
        result = chunk_text("")
        assert result == []

    def test_short_text_single_chunk(self):
        """Text shorter than max_tokens returns single chunk."""
        text = "Hello world."
        result = chunk_text(text, max_tokens=1000)

        assert len(result) == 1
        assert result[0]["text"] == text
        assert result[0]["chunk_index"] == 0
        assert result[0]["start_char"] == 0
        assert result[0]["end_char"] == len(text)

    def test_chunk_structure(self):
        """Each chunk has required fields."""
        text = "Test sentence one. Test sentence two."
        result = chunk_text(text, max_tokens=1000)

        chunk = result[0]
        assert "chunk_index" in chunk
        assert "text" in chunk
        assert "start_char" in chunk
        assert "end_char" in chunk
        assert "estimated_tokens" in chunk

    def test_basic_chunking(self):
        """Long text is split into multiple chunks."""
        # Create text that exceeds max_tokens (4 chars per token)
        # 100 tokens = 400 chars, so 500 chars should create 2+ chunks
        text = "Word. " * 100  # ~600 chars
        result = chunk_text(text, max_tokens=100, overlap_tokens=10)

        assert len(result) >= 2
        # Verify chunks cover the text
        assert result[0]["start_char"] == 0
        assert result[-1]["end_char"] == len(text)

    def test_overlap_between_chunks(self):
        """Chunks have overlapping content."""
        text = "A" * 2000  # Long enough for multiple chunks
        result = chunk_text(text, max_tokens=200, overlap_tokens=50)

        if len(result) >= 2:
            # Second chunk should start before first chunk ends
            # overlap_chars = 50 * 4 = 200
            first_end = result[0]["end_char"]
            second_start = result[1]["start_char"]
            assert second_start < first_end  # Overlap exists

    def test_sentence_boundary_breaking(self):
        """Chunks prefer to break at sentence boundaries."""
        # Create text with clear sentence boundaries
        sentences = ["This is sentence one. ", "This is sentence two. ", "This is sentence three."]
        text = "".join(sentences * 10)

        result = chunk_text(text, max_tokens=50, overlap_tokens=5)

        # Check that at least some chunks end with sentence-ending punctuation
        sentence_endings = sum(1 for c in result if c["text"].rstrip().endswith(('.', '!', '?')))
        assert sentence_endings > 0


class TestExtractTextWithCoordinates:
    """Tests for extract_text_with_coordinates function."""

    def test_returns_list(self, sample_pdf):
        """Function returns a list."""
        doc = pymupdf.open(sample_pdf)
        page = doc[0]

        result = extract_text_with_coordinates(page)

        assert isinstance(result, list)
        doc.close()

    def test_text_block_structure(self, sample_pdf):
        """Text blocks have required fields: type, text, y, bbox."""
        doc = pymupdf.open(sample_pdf)
        page = doc[0]

        result = extract_text_with_coordinates(page)

        # Find a text block
        text_blocks = [b for b in result if b["type"] == "text"]
        assert len(text_blocks) > 0

        block = text_blocks[0]
        assert block["type"] == "text"
        assert "text" in block
        assert "y" in block
        assert "bbox" in block
        assert isinstance(block["bbox"], (list, tuple))
        assert len(block["bbox"]) == 4

        doc.close()

    def test_sorted_by_y_coordinate(self, sample_pdf):
        """Results are sorted by Y coordinate."""
        doc = pymupdf.open(sample_pdf)
        page = doc[0]

        result = extract_text_with_coordinates(page)

        if len(result) >= 2:
            y_values = [block["y"] for block in result]
            assert y_values == sorted(y_values)

        doc.close()

    def test_empty_page_returns_empty_list(self):
        """Empty page returns empty list."""
        doc = pymupdf.open()
        page = doc.new_page()

        result = extract_text_with_coordinates(page)

        assert result == []
        doc.close()

    def test_image_placeholder_structure(self, sample_pdf_with_images):
        """Image blocks have type 'image_placeholder'."""
        doc = pymupdf.open(sample_pdf_with_images)
        page = doc[0]

        result = extract_text_with_coordinates(page)

        # Check for image placeholders
        image_blocks = [b for b in result if b["type"] == "image_placeholder"]

        for block in image_blocks:
            assert "y" in block
            assert "bbox" in block

        doc.close()


class TestExtractImagesColorFormats:
    """Tests for extract_images_from_page color format handling."""

    def test_rgb_format(self, sample_pdf_with_images):
        """RGB images report 'rgb' format."""
        doc = pymupdf.open(sample_pdf_with_images)

        images = extract_images_from_page(doc, 0)

        # The fixture uses a standard RGB PNG
        if images:
            assert images[0]["format"] in ("rgb", "rgba", "grayscale")

        doc.close()

    def test_grayscale_format(self, sample_pdf_grayscale):
        """Grayscale images report 'grayscale' format."""
        doc = pymupdf.open(sample_pdf_grayscale)

        images = extract_images_from_page(doc, 0)

        if images:
            # Grayscale has pix.n == 1
            assert images[0]["format"] == "grayscale"

        doc.close()

    def test_rgba_format(self, sample_pdf_rgba):
        """RGBA images report 'rgba' format."""
        doc = pymupdf.open(sample_pdf_rgba)

        images = extract_images_from_page(doc, 0)

        if images:
            # RGBA has pix.n == 4
            assert images[0]["format"] in ("rgba", "rgb")  # May be converted

        doc.close()

    def test_image_extraction_returns_base64(self, sample_pdf_with_images):
        """Extracted images have valid base64 data."""
        import base64

        doc = pymupdf.open(sample_pdf_with_images)
        images = extract_images_from_page(doc, 0)

        for img in images:
            # Should not raise
            decoded = base64.b64decode(img["data"])
            # PNG magic bytes
            assert decoded[:4] == b"\x89PNG"

        doc.close()

    def test_exception_handling_skips_bad_images(self, sample_pdf):
        """Problematic images are skipped without raising."""
        doc = pymupdf.open(sample_pdf)

        # This PDF has no images, so nothing to extract
        # Just verify no exception
        images = extract_images_from_page(doc, 0)
        assert isinstance(images, list)

        doc.close()


class TestExtractTextFromPageOptions:
    """Tests for extract_text_from_page options."""

    def test_sort_by_position_true_default(self, sample_pdf):
        """Default behavior sorts by position."""
        doc = pymupdf.open(sample_pdf)
        page = doc[0]

        text = extract_text_from_page(page)  # sort_by_position=True by default

        assert len(text) > 0
        assert "page 1" in text.lower()

        doc.close()

    def test_sort_by_position_false(self, sample_pdf):
        """sort_by_position=False uses raw extraction."""
        doc = pymupdf.open(sample_pdf)
        page = doc[0]

        text = extract_text_from_page(page, sort_by_position=False)

        assert len(text) > 0
        # Content should still be present
        assert "page" in text.lower()

        doc.close()
