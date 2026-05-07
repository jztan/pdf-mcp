"""
pdf_mcp.editor: PDF creation and manipulation primitives.

Pure functions used by edit_tools.py. Mirrors the role of extractor.py
for read-side operations.
"""
from __future__ import annotations

import subprocess
from io import BytesIO
from pathlib import Path
from typing import Any

from .config import PDFConfig
from .extractor import parse_page_range

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
COMPRESS_QUALITIES = {"screen", "ebook", "printer", "prepress", "default"}
PAGE_NUMBER_POSITIONS = {
    "bottom-center", "bottom-right", "bottom-left",
    "top-center", "top-right", "top-left",
}


def _resolve_input_image(path: str, pdf_config: PDFConfig) -> Path:
    """Validate that path is an existing image and passes path policy."""
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    resolved = p.resolve()
    if resolved.suffix.lower() not in SUPPORTED_IMAGE_EXTS:
        raise ValueError(
            f"Unsupported image extension {resolved.suffix}. "
            f"Supported: {sorted(SUPPORTED_IMAGE_EXTS)}"
        )
    pdf_config.check_path(str(resolved))
    if not resolved.exists():
        raise FileNotFoundError(f"Image not found: {path}")
    return resolved


def _resolve_output_pdf(
    path: str, pdf_config: PDFConfig, overwrite: bool
) -> Path:
    """Validate output PDF path: .pdf suffix, parent exists, policy passes."""
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    resolved = p.resolve()
    if resolved.suffix.lower() != ".pdf":
        raise ValueError(
            f"Output must end with .pdf, got: {resolved.suffix}"
        )
    pdf_config.check_path(str(resolved))
    if not resolved.parent.exists():
        raise FileNotFoundError(
            f"Parent directory does not exist: {resolved.parent}"
        )
    if resolved.exists() and not overwrite:
        raise FileExistsError(
            f"Output already exists: {resolved}. "
            f"Set overwrite=True to replace."
        )
    return resolved


def images_to_pdf(
    image_paths: list[str],
    output_path: str,
    pdf_config: PDFConfig,
    overwrite: bool = False,
) -> dict[str, Any]:
    """
    Combine one or more images (JPEG/PNG/BMP/TIFF/WebP) into a single PDF.

    Each image becomes one page. Images are converted to RGB if needed.
    Page size matches each image's pixel dimensions at 72 DPI.
    """
    from PIL import Image

    if not image_paths:
        raise ValueError("image_paths must contain at least one path")

    resolved_inputs = [
        _resolve_input_image(p, pdf_config) for p in image_paths
    ]
    output = _resolve_output_pdf(output_path, pdf_config, overwrite)

    rgb_images: list[Image.Image] = []
    try:
        for img_path in resolved_inputs:
            img = Image.open(img_path)
            if img.mode != "RGB":
                img = img.convert("RGB")
            rgb_images.append(img)

        first, rest = rgb_images[0], rgb_images[1:]
        first.save(
            output,
            "PDF",
            resolution=72.0,
            save_all=True,
            append_images=rest,
        )
    finally:
        for img in rgb_images:
            img.close()

    return {
        "output_path": str(output),
        "page_count": len(resolved_inputs),
        "input_count": len(resolved_inputs),
        "size_bytes": output.stat().st_size,
    }


def _resolve_pdf_input(path: str, pdf_config: PDFConfig) -> Path:
    """Validate that path is an existing PDF and passes path policy."""
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    resolved = p.resolve()
    if resolved.suffix.lower() != ".pdf":
        raise ValueError(
            f"Expected a .pdf file, got: {resolved.suffix}"
        )
    pdf_config.check_path(str(resolved))
    if not resolved.exists():
        raise FileNotFoundError(f"PDF not found: {path}")
    return resolved


def _resolve_output_dir(path: str, pdf_config: PDFConfig) -> Path:
    """Validate output directory: exists, is a dir, passes policy."""
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    resolved = p.resolve()
    pdf_config.check_path(str(resolved))
    if not resolved.exists():
        raise FileNotFoundError(f"Directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Not a directory: {resolved}")
    return resolved


def rename_pdf(
    source: str,
    destination: str,
    pdf_config: PDFConfig,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Rename or move a PDF file."""
    src = _resolve_pdf_input(source, pdf_config)
    dst = _resolve_output_pdf(destination, pdf_config, overwrite)
    src.rename(dst)
    return {
        "source": str(src),
        "destination": str(dst),
        "size_bytes": dst.stat().st_size,
    }


def merge_pdfs(
    input_paths: list[str],
    output_path: str,
    pdf_config: PDFConfig,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Merge multiple PDFs into one (in given order)."""
    from pypdf import PdfReader, PdfWriter

    if not input_paths:
        raise ValueError("input_paths must contain at least one path")

    inputs = [_resolve_pdf_input(p, pdf_config) for p in input_paths]
    output = _resolve_output_pdf(output_path, pdf_config, overwrite)

    writer = PdfWriter()
    page_counts: list[int] = []
    for in_path in inputs:
        reader = PdfReader(str(in_path))
        page_counts.append(len(reader.pages))
        for page in reader.pages:
            writer.add_page(page)
    with open(output, "wb") as f:
        writer.write(f)
    return {
        "output_path": str(output),
        "input_count": len(inputs),
        "page_counts_per_input": page_counts,
        "total_pages": sum(page_counts),
        "size_bytes": output.stat().st_size,
    }


def split_pdf(
    source: str,
    output_dir: str,
    pages_per_file: int,
    pdf_config: PDFConfig,
    prefix: str = "part",
) -> dict[str, Any]:
    """Split a PDF into chunks of N pages each."""
    from pypdf import PdfReader, PdfWriter

    if pages_per_file < 1:
        raise ValueError("pages_per_file must be >= 1")

    src = _resolve_pdf_input(source, pdf_config)
    out_dir = _resolve_output_dir(output_dir, pdf_config)

    reader = PdfReader(str(src))
    total = len(reader.pages)
    files_written: list[str] = []

    for chunk_idx, chunk_start in enumerate(range(0, total, pages_per_file)):
        chunk_end = min(chunk_start + pages_per_file, total)
        writer = PdfWriter()
        for i in range(chunk_start, chunk_end):
            writer.add_page(reader.pages[i])
        out_file = out_dir / f"{prefix}_{chunk_idx + 1:03d}.pdf"
        with open(out_file, "wb") as f:
            writer.write(f)
        files_written.append(str(out_file))

    return {
        "source": str(src),
        "output_files": files_written,
        "file_count": len(files_written),
        "total_pages": total,
        "pages_per_file": pages_per_file,
    }


def extract_pages(
    source: str,
    page_spec: str,
    output_path: str,
    pdf_config: PDFConfig,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Extract specific pages to a new PDF.

    page_spec accepts the same syntax as pdf_read_pages:
      "1-10", "1,5,10", "1-5,10,15-20"
    """
    from pypdf import PdfReader, PdfWriter

    src = _resolve_pdf_input(source, pdf_config)
    output = _resolve_output_pdf(output_path, pdf_config, overwrite)

    reader = PdfReader(str(src))
    total = len(reader.pages)
    page_nums = parse_page_range(page_spec, total)
    if not page_nums:
        raise ValueError(
            f"No valid pages in spec '{page_spec}' "
            f"for {total}-page document"
        )

    writer = PdfWriter()
    for pn in page_nums:
        writer.add_page(reader.pages[pn])
    with open(output, "wb") as f:
        writer.write(f)
    return {
        "source": str(src),
        "output_path": str(output),
        "extracted_pages": [pn + 1 for pn in page_nums],
        "page_count": len(page_nums),
        "size_bytes": output.stat().st_size,
    }


def remove_pages(
    source: str,
    page_spec: str,
    output_path: str,
    pdf_config: PDFConfig,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Remove specified pages, write remainder to new PDF."""
    from pypdf import PdfReader, PdfWriter

    src = _resolve_pdf_input(source, pdf_config)
    output = _resolve_output_pdf(output_path, pdf_config, overwrite)

    reader = PdfReader(str(src))
    total = len(reader.pages)
    page_nums = parse_page_range(page_spec, total)
    if not page_nums:
        raise ValueError(
            f"No valid pages in spec '{page_spec}' "
            f"for {total}-page document"
        )

    pages_to_remove = set(page_nums)
    if len(pages_to_remove) >= total:
        raise ValueError(
            "Cannot remove all pages; at least one page must remain"
        )

    writer = PdfWriter()
    kept: list[int] = []
    for i in range(total):
        if i not in pages_to_remove:
            writer.add_page(reader.pages[i])
            kept.append(i + 1)
    with open(output, "wb") as f:
        writer.write(f)
    return {
        "source": str(src),
        "output_path": str(output),
        "removed_pages": sorted(p + 1 for p in pages_to_remove),
        "kept_pages": kept,
        "page_count": len(kept),
        "size_bytes": output.stat().st_size,
    }


def rotate_pages(
    source: str,
    page_spec: str,
    angle: int,
    output_path: str,
    pdf_config: PDFConfig,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Rotate specified pages by 90, 180, or 270 degrees.

    page_spec="all" rotates every page.
    """
    from pypdf import PdfReader, PdfWriter

    if angle not in (90, 180, 270, -90, -180, -270):
        raise ValueError(
            f"angle must be 90, 180, or 270 (or negatives), got {angle}"
        )

    src = _resolve_pdf_input(source, pdf_config)
    output = _resolve_output_pdf(output_path, pdf_config, overwrite)

    reader = PdfReader(str(src))
    total = len(reader.pages)

    if page_spec == "all":
        page_nums = list(range(total))
    else:
        page_nums = parse_page_range(page_spec, total)
        if not page_nums:
            raise ValueError(
                f"No valid pages in spec '{page_spec}' "
                f"for {total}-page document"
            )

    pages_to_rotate = set(page_nums)
    writer = PdfWriter()
    for i in range(total):
        page = reader.pages[i]
        if i in pages_to_rotate:
            page.rotate(angle)
        writer.add_page(page)
    with open(output, "wb") as f:
        writer.write(f)
    return {
        "source": str(src),
        "output_path": str(output),
        "rotated_pages": [pn + 1 for pn in page_nums],
        "angle": angle,
        "size_bytes": output.stat().st_size,
    }


def compress_pdf(
    source: str,
    output_path: str,
    pdf_config: PDFConfig,
    quality: str = "ebook",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Compress a PDF using ghostscript.

    Quality profiles (gs -dPDFSETTINGS):
      screen   — 72 dpi, smallest
      ebook    — 150 dpi, balanced (default)
      printer  — 300 dpi
      prepress — 300 dpi, color preserving
      default  — gs default
    """
    if quality not in COMPRESS_QUALITIES:
        raise ValueError(
            f"quality must be one of {sorted(COMPRESS_QUALITIES)}, "
            f"got '{quality}'"
        )

    src = _resolve_pdf_input(source, pdf_config)
    output = _resolve_output_pdf(output_path, pdf_config, overwrite)

    cmd = [
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS=/{quality}",
        "-dNOPAUSE", "-dQUIET", "-dBATCH",
        f"-sOutputFile={output}",
        str(src),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ghostscript ('gs') not found. "
            "Install via: apt install ghostscript"
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"ghostscript failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )

    src_size = src.stat().st_size
    out_size = output.stat().st_size
    return {
        "source": str(src),
        "output_path": str(output),
        "quality_profile": quality,
        "original_size_bytes": src_size,
        "compressed_size_bytes": out_size,
        "reduction_percent": (
            round((1 - out_size / src_size) * 100, 2) if src_size > 0 else 0
        ),
    }


def ocr_pdf(
    source: str,
    output_path: str,
    pdf_config: PDFConfig,
    languages: str = "ara+fra+eng",
    force_ocr: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Run OCR on a PDF using ocrmypdf.

    Default languages 'ara+fra+eng' suit Tunisian legal documents.
    Set force_ocr=True to OCR even pages that already have text.
    """
    src = _resolve_pdf_input(source, pdf_config)
    output = _resolve_output_pdf(output_path, pdf_config, overwrite)

    cmd = ["ocrmypdf", "-l", languages]
    if force_ocr:
        cmd.append("--force-ocr")
    else:
        cmd.append("--skip-text")
    cmd.extend([str(src), str(output)])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ocrmypdf not found. Install via: apt install ocrmypdf"
        ) from exc

    # ocrmypdf success codes: 0 = OK, 6 = OK but had issues
    if result.returncode not in (0, 6):
        raise RuntimeError(
            f"ocrmypdf failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )

    return {
        "source": str(src),
        "output_path": str(output),
        "languages": languages,
        "force_ocr": force_ocr,
        "size_bytes": output.stat().st_size,
        "ocrmypdf_exit_code": result.returncode,
    }


def add_page_numbers(
    source: str,
    output_path: str,
    pdf_config: PDFConfig,
    position: str = "bottom-center",
    start_at: int = 1,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Stamp page numbers on each page using a reportlab overlay."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen.canvas import Canvas

    if position not in PAGE_NUMBER_POSITIONS:
        raise ValueError(
            f"position must be one of {sorted(PAGE_NUMBER_POSITIONS)}, "
            f"got '{position}'"
        )

    src = _resolve_pdf_input(source, pdf_config)
    output = _resolve_output_pdf(output_path, pdf_config, overwrite)

    reader = PdfReader(str(src))
    writer = PdfWriter()
    margin = 36  # 0.5 inch

    for idx, page in enumerate(reader.pages):
        page_num = start_at + idx
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)

        buf = BytesIO()
        c = Canvas(buf, pagesize=(w, h))
        c.setFont("Helvetica", 10)
        text = str(page_num)
        text_w = c.stringWidth(text, "Helvetica", 10)

        if position == "bottom-center":
            x, y = (w - text_w) / 2, margin
        elif position == "bottom-right":
            x, y = w - text_w - margin, margin
        elif position == "bottom-left":
            x, y = margin, margin
        elif position == "top-center":
            x, y = (w - text_w) / 2, h - margin
        elif position == "top-right":
            x, y = w - text_w - margin, h - margin
        else:  # top-left
            x, y = margin, h - margin

        c.drawString(x, y, text)
        c.save()
        buf.seek(0)

        overlay = PdfReader(buf).pages[0]
        writer.add_page(page)
        writer.pages[-1].merge_page(overlay)

    with open(output, "wb") as f:
        writer.write(f)
    return {
        "source": str(src),
        "output_path": str(output),
        "position": position,
        "start_at": start_at,
        "page_count": len(reader.pages),
        "size_bytes": output.stat().st_size,
    }


def add_watermark(
    source: str,
    output_path: str,
    text: str,
    pdf_config: PDFConfig,
    opacity: float = 0.3,
    font_size: int = 60,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Add a diagonal text watermark to every page."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen.canvas import Canvas

    if not text.strip():
        raise ValueError("watermark text cannot be empty")
    if not 0.0 <= opacity <= 1.0:
        raise ValueError(f"opacity must be in [0.0, 1.0], got {opacity}")
    if font_size <= 0:
        raise ValueError(f"font_size must be > 0, got {font_size}")

    src = _resolve_pdf_input(source, pdf_config)
    output = _resolve_output_pdf(output_path, pdf_config, overwrite)

    reader = PdfReader(str(src))
    writer = PdfWriter()

    for page in reader.pages:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)

        buf = BytesIO()
        c = Canvas(buf, pagesize=(w, h))
        c.setFont("Helvetica-Bold", font_size)
        c.setFillGray(0.5, opacity)
        c.saveState()
        c.translate(w / 2, h / 2)
        c.rotate(45)
        text_w = c.stringWidth(text, "Helvetica-Bold", font_size)
        c.drawString(-text_w / 2, 0, text)
        c.restoreState()
        c.save()
        buf.seek(0)

        overlay = PdfReader(buf).pages[0]
        writer.add_page(page)
        writer.pages[-1].merge_page(overlay)

    with open(output, "wb") as f:
        writer.write(f)
    return {
        "source": str(src),
        "output_path": str(output),
        "watermark_text": text,
        "opacity": opacity,
        "font_size": font_size,
        "page_count": len(reader.pages),
        "size_bytes": output.stat().st_size,
    }
