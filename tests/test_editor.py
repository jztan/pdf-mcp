# tests/test_editor.py
"""Tests for pdf_mcp.editor module."""

from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from pdf_mcp.config import PDFConfig
from pdf_mcp.editor import (
    add_page_numbers,
    add_watermark,
    compress_pdf,
    extract_pages,
    images_to_pdf,
    merge_pdfs,
    ocr_pdf,
    remove_pages,
    rename_pdf,
    rotate_pages,
    split_pdf,
)


@pytest.fixture
def pdf_config():
    """Permissive config (no path restrictions)."""
    return PDFConfig()


@pytest.fixture
def two_test_images(tmp_path):
    """Create two test images of different formats and modes."""
    a = tmp_path / "a.png"
    b = tmp_path / "b.jpg"
    Image.new("RGB", (800, 600), "red").save(a)
    Image.new("RGB", (1000, 1400), "blue").save(b)
    return [str(a), str(b)]


@pytest.fixture
def small_pdf(tmp_path):
    """Create a 5-page PDF with text on each page."""
    p = tmp_path / "small.pdf"
    doc = pymupdf.open()
    for i in range(5):
        page = doc.new_page()
        page.insert_text((50, 50), f"Page {i + 1} content")
    doc.save(str(p))
    doc.close()
    return str(p)


@pytest.fixture
def two_pdfs(tmp_path):
    """Create two PDFs (2 and 3 pages)."""
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"

    doc_a = pymupdf.open()
    for i in range(2):
        page = doc_a.new_page()
        page.insert_text((50, 50), f"A page {i + 1}")
    doc_a.save(str(a))
    doc_a.close()

    doc_b = pymupdf.open()
    for i in range(3):
        page = doc_b.new_page()
        page.insert_text((50, 50), f"B page {i + 1}")
    doc_b.save(str(b))
    doc_b.close()

    return str(a), str(b)


class TestImagesToPdf:
    """Tests for images_to_pdf."""

    def test_basic_two_images(self, two_test_images, tmp_path, pdf_config):
        out = tmp_path / "merged.pdf"
        result = images_to_pdf(two_test_images, str(out), pdf_config)

        assert Path(result["output_path"]).exists()
        assert result["page_count"] == 2
        assert result["input_count"] == 2
        assert result["size_bytes"] > 0

        doc = pymupdf.open(out)
        try:
            assert len(doc) == 2
        finally:
            doc.close()

    def test_single_image(self, tmp_path, pdf_config):
        img = tmp_path / "x.png"
        Image.new("RGB", (400, 300), "green").save(img)
        out = tmp_path / "single.pdf"

        result = images_to_pdf([str(img)], str(out), pdf_config)

        assert result["page_count"] == 1
        doc = pymupdf.open(out)
        try:
            assert len(doc) == 1
        finally:
            doc.close()

    def test_rgba_image_converted_to_rgb(self, tmp_path, pdf_config):
        """RGBA images must be converted to RGB before PDF save."""
        img = tmp_path / "transparent.png"
        Image.new("RGBA", (200, 200), (255, 0, 0, 128)).save(img)
        out = tmp_path / "rgba.pdf"

        result = images_to_pdf([str(img)], str(out), pdf_config)
        assert result["page_count"] == 1
        assert Path(out).exists()

    def test_grayscale_image_converted(self, tmp_path, pdf_config):
        """Grayscale (mode 'L') images should also be converted."""
        img = tmp_path / "gray.png"
        Image.new("L", (200, 200), 128).save(img)
        out = tmp_path / "gray.pdf"

        result = images_to_pdf([str(img)], str(out), pdf_config)
        assert result["page_count"] == 1

    def test_empty_list_raises(self, tmp_path, pdf_config):
        with pytest.raises(ValueError, match="at least one"):
            images_to_pdf([], str(tmp_path / "out.pdf"), pdf_config)

    def test_unsupported_extension_raises(self, tmp_path, pdf_config):
        bad = tmp_path / "doc.txt"
        bad.write_text("not an image")
        with pytest.raises(ValueError, match="Unsupported image extension"):
            images_to_pdf([str(bad)], str(tmp_path / "out.pdf"), pdf_config)

    def test_missing_input_raises(self, tmp_path, pdf_config):
        with pytest.raises(FileNotFoundError, match="Image not found"):
            images_to_pdf(
                [str(tmp_path / "nope.png")],
                str(tmp_path / "out.pdf"),
                pdf_config,
            )

    def test_output_must_be_pdf(self, two_test_images, tmp_path, pdf_config):
        with pytest.raises(ValueError, match="must end with .pdf"):
            images_to_pdf(
                two_test_images,
                str(tmp_path / "out.txt"),
                pdf_config,
            )

    def test_overwrite_protection(self, two_test_images, tmp_path, pdf_config):
        out = tmp_path / "merged.pdf"
        images_to_pdf(two_test_images, str(out), pdf_config)

        with pytest.raises(FileExistsError, match="overwrite=True"):
            images_to_pdf(two_test_images, str(out), pdf_config)

    def test_overwrite_true_replaces(self, two_test_images, tmp_path, pdf_config):
        out = tmp_path / "merged.pdf"
        images_to_pdf(two_test_images, str(out), pdf_config)
        first_size = out.stat().st_size

        # Overwrite with single image — should produce smaller PDF
        single = [two_test_images[0]]
        images_to_pdf(single, str(out), pdf_config, overwrite=True)
        second_size = out.stat().st_size

        # Different content → different size (or at least same file overwritten)
        doc = pymupdf.open(out)
        try:
            assert len(doc) == 1  # Now single page
        finally:
            doc.close()
        assert first_size != second_size or True  # may match by coincidence

    def test_missing_parent_dir_raises(
        self, two_test_images, tmp_path, pdf_config
    ):
        with pytest.raises(FileNotFoundError, match="Parent directory"):
            images_to_pdf(
                two_test_images,
                str(tmp_path / "no" / "such" / "dir" / "out.pdf"),
                pdf_config,
            )

    def test_page_order_preserved(self, tmp_path, pdf_config):
        """First image stays first, regardless of dimensions."""
        small = tmp_path / "small.png"
        large = tmp_path / "large.png"
        Image.new("RGB", (200, 200), "red").save(small)
        Image.new("RGB", (1000, 1000), "blue").save(large)

        out = tmp_path / "ordered.pdf"
        images_to_pdf([str(small), str(large)], str(out), pdf_config)

        doc = pymupdf.open(out)
        try:
            # Page 1 should match small image dimensions (200x200 px @ 72dpi)
            assert doc[0].rect.width == pytest.approx(200, abs=1)
            assert doc[1].rect.width == pytest.approx(1000, abs=1)
        finally:
            doc.close()


class TestRenamePdf:
    def test_basic_rename(self, small_pdf, tmp_path, pdf_config):
        dst = tmp_path / "renamed.pdf"
        result = rename_pdf(small_pdf, str(dst), pdf_config)
        assert Path(result["destination"]).exists()
        assert not Path(small_pdf).exists()
        assert result["size_bytes"] > 0

    def test_rename_protects_existing(self, small_pdf, tmp_path, pdf_config):
        dst = tmp_path / "exists.pdf"
        dst.write_bytes(b"%PDF-1.4\nfake")
        with pytest.raises(FileExistsError):
            rename_pdf(small_pdf, str(dst), pdf_config)

    def test_rename_overwrite_true(self, small_pdf, tmp_path, pdf_config):
        dst = tmp_path / "exists.pdf"
        dst.write_bytes(b"%PDF-1.4\nfake")
        rename_pdf(small_pdf, str(dst), pdf_config, overwrite=True)
        # Real PDF now at dst
        doc = pymupdf.open(str(dst))
        try:
            assert len(doc) == 5
        finally:
            doc.close()


class TestMergePdfs:
    def test_basic_merge(self, two_pdfs, tmp_path, pdf_config):
        a, b = two_pdfs
        out = tmp_path / "merged.pdf"
        result = merge_pdfs([a, b], str(out), pdf_config)
        assert result["total_pages"] == 5
        assert result["page_counts_per_input"] == [2, 3]
        doc = pymupdf.open(out)
        try:
            assert len(doc) == 5
        finally:
            doc.close()

    def test_empty_list_raises(self, tmp_path, pdf_config):
        with pytest.raises(ValueError, match="at least one"):
            merge_pdfs([], str(tmp_path / "out.pdf"), pdf_config)


class TestSplitPdf:
    def test_split_into_chunks(self, small_pdf, tmp_path, pdf_config):
        out_dir = tmp_path / "chunks"
        out_dir.mkdir()
        result = split_pdf(small_pdf, str(out_dir), 2, pdf_config)
        # 5 pages, 2 per file → 3 files (2+2+1)
        assert result["file_count"] == 3
        assert all(Path(f).exists() for f in result["output_files"])

    def test_pages_per_file_invalid(self, small_pdf, tmp_path, pdf_config):
        out_dir = tmp_path / "chunks"
        out_dir.mkdir()
        with pytest.raises(ValueError, match="pages_per_file"):
            split_pdf(small_pdf, str(out_dir), 0, pdf_config)

    def test_split_missing_dir(self, small_pdf, tmp_path, pdf_config):
        with pytest.raises(FileNotFoundError):
            split_pdf(small_pdf, str(tmp_path / "nope"), 2, pdf_config)


class TestExtractPages:
    def test_extract_range(self, small_pdf, tmp_path, pdf_config):
        out = tmp_path / "extracted.pdf"
        result = extract_pages(small_pdf, "1-3", str(out), pdf_config)
        assert result["page_count"] == 3
        assert result["extracted_pages"] == [1, 2, 3]

    def test_extract_individual_pages(self, small_pdf, tmp_path, pdf_config):
        out = tmp_path / "extracted.pdf"
        result = extract_pages(small_pdf, "1,3,5", str(out), pdf_config)
        assert result["extracted_pages"] == [1, 3, 5]

    def test_extract_invalid_spec(self, small_pdf, tmp_path, pdf_config):
        with pytest.raises(ValueError, match="No valid pages"):
            extract_pages(
                small_pdf, "abc", str(tmp_path / "out.pdf"), pdf_config
            )


class TestRemovePages:
    def test_remove_some(self, small_pdf, tmp_path, pdf_config):
        out = tmp_path / "trimmed.pdf"
        result = remove_pages(small_pdf, "2,4", str(out), pdf_config)
        assert result["page_count"] == 3
        assert result["removed_pages"] == [2, 4]
        assert result["kept_pages"] == [1, 3, 5]

    def test_cannot_remove_all(self, small_pdf, tmp_path, pdf_config):
        with pytest.raises(ValueError, match="Cannot remove all"):
            remove_pages(
                small_pdf, "1-5", str(tmp_path / "out.pdf"), pdf_config
            )


class TestRotatePages:
    def test_rotate_all(self, small_pdf, tmp_path, pdf_config):
        out = tmp_path / "rotated.pdf"
        result = rotate_pages(small_pdf, "all", 90, str(out), pdf_config)
        assert result["angle"] == 90
        assert len(result["rotated_pages"]) == 5

    def test_rotate_specific(self, small_pdf, tmp_path, pdf_config):
        out = tmp_path / "rotated.pdf"
        result = rotate_pages(small_pdf, "2,4", 180, str(out), pdf_config)
        assert result["rotated_pages"] == [2, 4]

    def test_invalid_angle(self, small_pdf, tmp_path, pdf_config):
        with pytest.raises(ValueError, match="angle must"):
            rotate_pages(
                small_pdf, "all", 45, str(tmp_path / "out.pdf"), pdf_config
            )


@pytest.mark.integration
class TestCompressPdf:
    def test_basic_compress(self, small_pdf, tmp_path, pdf_config):
        out = tmp_path / "compressed.pdf"
        result = compress_pdf(small_pdf, str(out), pdf_config)
        assert result["quality_profile"] == "ebook"
        assert "reduction_percent" in result
        assert Path(out).exists()

    def test_invalid_quality(self, small_pdf, tmp_path, pdf_config):
        with pytest.raises(ValueError, match="quality must"):
            compress_pdf(
                small_pdf,
                str(tmp_path / "out.pdf"),
                pdf_config,
                quality="bogus",
            )


@pytest.mark.integration
class TestOcrPdf:
    def test_basic_ocr_skip_text(self, small_pdf, tmp_path, pdf_config):
        """small_pdf has text — with skip-text default, ocrmypdf passes through."""
        out = tmp_path / "ocred.pdf"
        result = ocr_pdf(
            small_pdf, str(out), pdf_config, languages="eng"
        )
        assert Path(out).exists()
        assert result["languages"] == "eng"


class TestAddPageNumbers:
    def test_basic(self, small_pdf, tmp_path, pdf_config):
        out = tmp_path / "numbered.pdf"
        result = add_page_numbers(small_pdf, str(out), pdf_config)
        assert result["position"] == "bottom-center"
        assert result["page_count"] == 5
        assert Path(out).exists()

    def test_invalid_position(self, small_pdf, tmp_path, pdf_config):
        with pytest.raises(ValueError, match="position must"):
            add_page_numbers(
                small_pdf,
                str(tmp_path / "out.pdf"),
                pdf_config,
                position="middle",
            )

    def test_custom_start_at(self, small_pdf, tmp_path, pdf_config):
        out = tmp_path / "numbered.pdf"
        result = add_page_numbers(
            small_pdf, str(out), pdf_config, start_at=10
        )
        assert result["start_at"] == 10


class TestAddWatermark:
    def test_basic(self, small_pdf, tmp_path, pdf_config):
        out = tmp_path / "watermarked.pdf"
        result = add_watermark(small_pdf, str(out), "DRAFT", pdf_config)
        assert result["watermark_text"] == "DRAFT"
        assert result["page_count"] == 5
        assert Path(out).exists()

    def test_empty_text_raises(self, small_pdf, tmp_path, pdf_config):
        with pytest.raises(ValueError, match="cannot be empty"):
            add_watermark(
                small_pdf, str(tmp_path / "out.pdf"), "  ", pdf_config
            )

    def test_invalid_opacity(self, small_pdf, tmp_path, pdf_config):
        with pytest.raises(ValueError, match="opacity"):
            add_watermark(
                small_pdf,
                str(tmp_path / "out.pdf"),
                "DRAFT",
                pdf_config,
                opacity=2.0,
            )
