"""
pdf_mcp.edit_tools: MCP tool wrappers for PDF creation/manipulation.

These tools are registered via register_edit_tools(mcp, pdf_config) called
from server.py. Mirrors the read-side tools defined directly in server.py.
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from . import editor
from .config import PDFConfig


def register_edit_tools(mcp: FastMCP, pdf_config: PDFConfig) -> None:
    """
    Register all PDF editing tools on the given FastMCP server.

    Called once from server.py after the mcp instance is created.
    """

    @mcp.tool()
    def pdf_from_images(
        image_paths: list[str],
        output_path: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Combine one or more images into a single PDF (one image = one page).

        Supported formats: JPEG, PNG, BMP, TIFF, WebP. Images are converted
        to RGB. Useful for assembling scanned documents (e.g. legal papers
        photographed page-by-page) into a single PDF.

        Args:
            image_paths: Ordered list of image file paths. Each becomes a
                page in the output PDF, in the order provided.
            output_path: Path where the resulting PDF will be written. Must
                end with .pdf. Parent directory must already exist.
            overwrite: If False (default), raises if output_path already
                exists. Set to True to replace.

        Returns:
            - output_path: Absolute path of the written PDF
            - page_count: Number of pages in the resulting PDF
            - input_count: Number of input images processed
            - size_bytes: File size of the output PDF
        """
        try:
            return editor.images_to_pdf(
                image_paths=image_paths,
                output_path=output_path,
                pdf_config=pdf_config,
                overwrite=overwrite,
            )
        except (
            ValueError,
            FileNotFoundError,
            FileExistsError,
        ) as exc:
            return {
                "error": str(exc),
                "error_type": type(exc).__name__,
            }

    @mcp.tool()
    def pdf_rename(
        source: str,
        destination: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Rename or move a PDF file on disk.

        Args:
            source: Path to the existing PDF.
            destination: New path. Must end with .pdf. Parent directory
                must already exist.
            overwrite: If False (default), raises if destination exists.

        Returns:
            - source: Original absolute path
            - destination: New absolute path
            - size_bytes: File size after rename
        """
        try:
            return editor.rename_pdf(
                source=source,
                destination=destination,
                pdf_config=pdf_config,
                overwrite=overwrite,
            )
        except (ValueError, FileNotFoundError, FileExistsError) as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool()
    def pdf_merge(
        input_paths: list[str],
        output_path: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Merge multiple PDFs into a single PDF, in the order provided.

        Args:
            input_paths: Ordered list of PDF paths to concatenate.
            output_path: Path for the merged PDF. Must end with .pdf.
            overwrite: If False (default), raises if output_path exists.

        Returns:
            - output_path, input_count, total_pages
            - page_counts_per_input: Pages in each input
            - size_bytes
        """
        try:
            return editor.merge_pdfs(
                input_paths=input_paths,
                output_path=output_path,
                pdf_config=pdf_config,
                overwrite=overwrite,
            )
        except (ValueError, FileNotFoundError, FileExistsError) as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool()
    def pdf_split(
        source: str,
        output_dir: str,
        pages_per_file: int,
        prefix: str = "part",
    ) -> dict[str, Any]:
        """
        Split a PDF into chunks of N pages each.

        Output files are named {prefix}_001.pdf, {prefix}_002.pdf, etc.

        Args:
            source: Path to the source PDF.
            output_dir: Existing directory where chunks are written.
            pages_per_file: Number of pages per chunk (>=1).
            prefix: Filename prefix for chunks (default 'part').

        Returns:
            - output_files: List of paths written
            - file_count, total_pages, pages_per_file
        """
        try:
            return editor.split_pdf(
                source=source,
                output_dir=output_dir,
                pages_per_file=pages_per_file,
                pdf_config=pdf_config,
                prefix=prefix,
            )
        except (
            ValueError,
            FileNotFoundError,
            NotADirectoryError,
        ) as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool()
    def pdf_extract_pages(
        source: str,
        page_spec: str,
        output_path: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Extract specific pages from a PDF into a new PDF.

        Args:
            source: Path to the source PDF.
            page_spec: Page specification — same syntax as pdf_read_pages
                ('1-10', '1,5,10', '1-5,10,15-20').
            output_path: Path for the extracted-pages PDF (.pdf).
            overwrite: If False (default), raises if output_path exists.

        Returns:
            - extracted_pages: List of 1-indexed pages copied
            - page_count, output_path, size_bytes
        """
        try:
            return editor.extract_pages(
                source=source,
                page_spec=page_spec,
                output_path=output_path,
                pdf_config=pdf_config,
                overwrite=overwrite,
            )
        except (ValueError, FileNotFoundError, FileExistsError) as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool()
    def pdf_remove_pages(
        source: str,
        page_spec: str,
        output_path: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Remove specified pages from a PDF, write the rest to a new PDF.

        Args:
            source: Path to the source PDF.
            page_spec: Pages to remove (same syntax as pdf_read_pages).
            output_path: Path for the trimmed PDF (.pdf).
            overwrite: If False (default), raises if output_path exists.

        Returns:
            - removed_pages, kept_pages (both 1-indexed)
            - page_count of result, output_path, size_bytes
        """
        try:
            return editor.remove_pages(
                source=source,
                page_spec=page_spec,
                output_path=output_path,
                pdf_config=pdf_config,
                overwrite=overwrite,
            )
        except (ValueError, FileNotFoundError, FileExistsError) as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool()
    def pdf_rotate_pages(
        source: str,
        page_spec: str,
        angle: int,
        output_path: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Rotate specified pages of a PDF by 90, 180, or 270 degrees.

        Use page_spec='all' to rotate every page.

        Args:
            source: Path to the source PDF.
            page_spec: Pages to rotate ('all', '1-3', '2,5', etc.).
            angle: 90, 180, 270 (clockwise) or negatives (counter-clockwise).
            output_path: Path for the rotated PDF.
            overwrite: If False (default), raises if output_path exists.

        Returns:
            - rotated_pages (1-indexed), angle, output_path, size_bytes
        """
        try:
            return editor.rotate_pages(
                source=source,
                page_spec=page_spec,
                angle=angle,
                output_path=output_path,
                pdf_config=pdf_config,
                overwrite=overwrite,
            )
        except (ValueError, FileNotFoundError, FileExistsError) as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool()
    def pdf_compress(
        source: str,
        output_path: str,
        quality: str = "ebook",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Compress a PDF using ghostscript.

        Quality profiles:
            screen   — 72 dpi, smallest file
            ebook    — 150 dpi, balanced (default)
            printer  — 300 dpi
            prepress — 300 dpi, color preserving
            default  — gs default

        Args:
            source: Path to the source PDF.
            output_path: Path for the compressed PDF.
            quality: One of 'screen', 'ebook', 'printer', 'prepress',
                'default'.
            overwrite: If False (default), raises if output_path exists.

        Returns:
            - quality_profile, original_size_bytes, compressed_size_bytes
            - reduction_percent (negative if file grew)
            - output_path
        """
        try:
            return editor.compress_pdf(
                source=source,
                output_path=output_path,
                pdf_config=pdf_config,
                quality=quality,
                overwrite=overwrite,
            )
        except (
            ValueError,
            FileNotFoundError,
            FileExistsError,
            RuntimeError,
        ) as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool()
    def pdf_ocr(
        source: str,
        output_path: str,
        languages: str = "ara+fra+eng",
        force_ocr: bool = False,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Run OCR on a PDF using ocrmypdf.

        Default languages 'ara+fra+eng' suit Tunisian legal documents.
        Pages that already have text are skipped unless force_ocr=True.

        Args:
            source: Path to the source PDF.
            output_path: Path for the OCR'd PDF.
            languages: Tesseract language codes joined with '+'
                (e.g. 'ara+fra+eng', 'eng', 'fra').
            force_ocr: Re-OCR pages that already contain text.
            overwrite: If False (default), raises if output_path exists.

        Returns:
            - languages, force_ocr, output_path, size_bytes
            - ocrmypdf_exit_code (0 = clean, 6 = OK with warnings)
        """
        try:
            return editor.ocr_pdf(
                source=source,
                output_path=output_path,
                pdf_config=pdf_config,
                languages=languages,
                force_ocr=force_ocr,
                overwrite=overwrite,
            )
        except (
            ValueError,
            FileNotFoundError,
            FileExistsError,
            RuntimeError,
        ) as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool()
    def pdf_add_page_numbers(
        source: str,
        output_path: str,
        position: str = "bottom-center",
        start_at: int = 1,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Stamp page numbers onto each page of a PDF.

        Args:
            source: Path to the source PDF.
            output_path: Path for the numbered PDF.
            position: One of 'bottom-center', 'bottom-right', 'bottom-left',
                'top-center', 'top-right', 'top-left'.
            start_at: First page's number (default 1).
            overwrite: If False (default), raises if output_path exists.

        Returns:
            - position, start_at, page_count, output_path, size_bytes
        """
        try:
            return editor.add_page_numbers(
                source=source,
                output_path=output_path,
                pdf_config=pdf_config,
                position=position,
                start_at=start_at,
                overwrite=overwrite,
            )
        except (ValueError, FileNotFoundError, FileExistsError) as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool()
    def pdf_add_watermark(
        source: str,
        output_path: str,
        text: str,
        opacity: float = 0.3,
        font_size: int = 60,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Add a diagonal text watermark to every page of a PDF.

        Args:
            source: Path to the source PDF.
            output_path: Path for the watermarked PDF.
            text: Watermark text (e.g. 'COPIE', 'DRAFT', 'CONFIDENTIEL').
            opacity: 0.0 (invisible) to 1.0 (opaque). Default 0.3.
            font_size: Helvetica-Bold size in pt. Default 60.
            overwrite: If False (default), raises if output_path exists.

        Returns:
            - watermark_text, opacity, font_size, page_count, output_path,
              size_bytes
        """
        try:
            return editor.add_watermark(
                source=source,
                output_path=output_path,
                text=text,
                pdf_config=pdf_config,
                opacity=opacity,
                font_size=font_size,
                overwrite=overwrite,
            )
        except (ValueError, FileNotFoundError, FileExistsError) as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}
