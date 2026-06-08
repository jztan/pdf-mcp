"""
PDF extraction utilities using PyMuPDF.
"""

import logging
import os
import re
import sys
import typing
import warnings
from pathlib import Path
from typing import Any

# Suppress PyMuPDF/SWIG DeprecationWarnings (upstream issue, not actionable).
# Python-level filter handles import-time warnings.
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message="builtin type.*[Ss]wig.*has no __module__ attribute",
)


# C-level SWIG warnings emitted during interpreter shutdown bypass Python's
# warning filters and write directly to stderr. Wrap stderr to catch those.
class _StderrSwigFilter:
    __slots__ = ("_stream",)

    def __init__(self, stream: typing.TextIO) -> None:
        self._stream = stream

    def write(self, msg: str) -> int:
        if "DeprecationWarning" in msg and "swig" in msg.lower():
            return len(msg)
        return self._stream.write(msg)

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


sys.stderr = _StderrSwigFilter(sys.stderr)  # type: ignore[assignment]

import pymupdf  # noqa: E402

from .parallel import PageError  # noqa: E402

logger = logging.getLogger(__name__)


def parse_page_range(pages: str | list[int] | None, total_pages: int) -> list[int]:
    """
    Parse page specification into list of 0-indexed page numbers.

    Args:
        pages: Page specification:
            - None: all pages
            - list[int]: explicit page numbers (1-indexed)
            - str: range like "1-5,10,15-20" (1-indexed)
        total_pages: Total number of pages in document

    Returns:
        List of 0-indexed page numbers

    Examples:
        >>> parse_page_range(None, 10)
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
        >>> parse_page_range([1, 5, 10], 10)
        [0, 4, 9]
        >>> parse_page_range("1-3,5,8-10", 10)
        [0, 1, 2, 4, 7, 8, 9]
    """
    if pages is None:
        return list(range(total_pages))

    if isinstance(pages, list):
        # Convert 1-indexed to 0-indexed
        return [p - 1 for p in pages if 1 <= p <= total_pages]

    # Parse string format like "1-5,10,15-20"
    result = []
    parts = re.split(r"[,\s]+", pages.strip())

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            # Range: "1-5" or "10-20"
            match = re.match(r"(\d+)\s*-\s*(\d+)", part)
            if match:
                start, end = int(match.group(1)), int(match.group(2))
                # Convert to 0-indexed and clamp to valid range
                for p in range(start - 1, end):
                    if 0 <= p < total_pages:
                        result.append(p)
        else:
            # Single page: "5"
            try:
                p = int(part) - 1  # Convert to 0-indexed
                if 0 <= p < total_pages:
                    result.append(p)
            except ValueError:
                continue

    # Remove duplicates while preserving order
    seen = set()
    unique_result = []
    for p in result:
        if p not in seen:
            seen.add(p)
            unique_result.append(p)

    return unique_result


def detect_column_boxes(page: Any) -> list[Any]:
    """Return column bounding boxes in reading order, or [] if unavailable.

    Wraps pymupdf4llm's column detector. Any failure — missing dependency,
    its version-guard ImportError, or a detection error — degrades to [] so
    callers fall back to positional-sort extraction.
    """
    try:
        from pymupdf4llm.helpers.multi_column import column_boxes

        # margins=0 keeps running headers/footers/page numbers in the column
        # boxes, matching the single-column path (which extracts the full page).
        # Verified to not affect reading-order benchmark score.
        return list(column_boxes(page, footer_margin=0, header_margin=0))
    except Exception:
        return []


# A page is only treated as multi-column when at least two detected boxes are
# "tall" — i.e. their height is at least this fraction of the tallest box on the
# page. Genuine text columns run most of the page height; a sparse grid of
# short cells (e.g. an academic paper's author/affiliation block laid out in a
# visual grid above a full-width body) is NOT a reading-order column structure,
# and extracting it column-by-column scrambles the intended row-by-row order.
# 0.25 sits comfortably above the ratio such grids produce (the Transformer
# title page's tallest author cell is ~0.22 of its full-width body box) while
# staying well below genuine half-height columns.
_COLUMN_MIN_HEIGHT_FRAC = 0.25


def _is_multi_column_layout(boxes: list[Any]) -> bool:
    """True only when >=2 detected boxes are tall enough to be real columns.

    Guards against ``detect_column_boxes`` over-segmenting a single-column page
    whose top is a visual grid (author/affiliation blocks, badge rows) into many
    short side-by-side boxes — reading those column-by-column reorders content
    that is meant to be read row-by-row. See ``_COLUMN_MIN_HEIGHT_FRAC``.
    """
    if len(boxes) <= 1:
        return False
    max_height = max(box.height for box in boxes)
    if max_height <= 0:
        return False
    tall = sum(1 for box in boxes if box.height >= _COLUMN_MIN_HEIGHT_FRAC * max_height)
    return tall >= 2


def extract_text_from_page(page: Any, sort_by_position: bool = True) -> str:
    """
    Extract text from a PDF page.

    Args:
        page: PyMuPDF page object
        sort_by_position: If True, sort text blocks by Y-coordinate for reading order

    Returns:
        Extracted text content
    """
    if sort_by_position:
        boxes = detect_column_boxes(page)
        if _is_multi_column_layout(boxes):
            # Multi-column: extract each column in reading order so the
            # text is not interleaved row-by-row across columns.
            parts = (
                page.get_text("text", clip=box, sort=True).strip() for box in boxes
            )
            return "\n\n".join(part for part in parts if part)
        # Single-column (or detection unavailable): positional block sort.
        blocks = page.get_text("blocks", sort=True)
        # blocks format: (x0, y0, x1, y1, "text", block_no, block_type)
        # block_type: 0 = text, 1 = image
        text_blocks = [block[4] for block in blocks if block[6] == 0]
        return "\n\n".join(text_blocks)
    else:
        return str(page.get_text())


_PARAGRAPH_MAX_CHARS = 2000


def get_paragraph_for_offset(
    page: Any, char_offset: int, max_chars: int = _PARAGRAPH_MAX_CHARS
) -> tuple[str | None, int | None]:
    """
    Find the text block containing char_offset in the page's joined text.

    The joined text uses the same layout as extract_text_from_page
    (blocks joined by "\\n\\n", text blocks only, sorted by position).

    Returns (block_text, block_index) or (None, None) if the offset
    is out of range or the matching block exceeds max_chars.
    """
    blocks = page.get_text("blocks", sort=True)
    text_blocks = [block[4] for block in blocks if block[6] == 0]

    cursor = 0
    for idx, block_text in enumerate(text_blocks):
        block_len = len(block_text)
        if cursor + block_len > char_offset:
            stripped = block_text.strip()
            if len(stripped) > max_chars:
                return None, None
            return stripped, idx
        cursor += block_len + 2  # +2 for "\n\n" separator

    return None, None


_PARAGRAPH_MIN_CHARS = 80


def get_best_paragraph_for_query(
    page: Any,
    query: str,
    max_chars: int = _PARAGRAPH_MAX_CHARS,
    min_chars: int = 0,
) -> tuple[str | None, int | None]:
    """
    Find the text block on *page* best matching *query* by token overlap.

    Scores each block by the count of distinct query tokens found
    (case-insensitive substring) and returns the highest-scoring block.
    Blocks shorter than *min_chars* (after stripping) are skipped —
    this filters out section headings and figure captions that score
    well on token overlap but carry no useful context.

    Works well for keyword and hybrid modes where query terms appear
    literally in the text.  For pure semantic queries (conceptual
    paraphrases with few literal tokens), the winning block may be
    topically related but not the strongest semantic match on the page.

    Returns (block_text, block_index) or (None, None) if no tokens
    match or the best block exceeds max_chars.
    """
    tokens = [t.strip(".,;:!?\"'()[]{}") for t in query.lower().split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return None, None

    blocks = page.get_text("blocks", sort=True)
    text_blocks = [block[4] for block in blocks if block[6] == 0]

    best_score = 0
    best_idx: int | None = None
    best_text: str | None = None

    for idx, raw_text in enumerate(text_blocks):
        stripped = raw_text.strip()
        if len(stripped) < min_chars:
            continue
        lower = raw_text.lower()
        score = sum(1 for t in tokens if t in lower)
        if score > best_score:
            best_score = score
            best_idx = idx
            best_text = raw_text

    if best_score == 0 or best_text is None:
        return None, None

    stripped = best_text.strip()
    if len(stripped) > max_chars:
        return None, None

    return stripped, best_idx


def extract_text_with_coordinates(page: Any) -> list[dict[str, Any]]:
    """
    Extract text with Y-coordinate information for content ordering.

    Args:
        page: PyMuPDF page object

    Returns:
        List of content blocks with type, text, and position
    """
    blocks = page.get_text("dict")["blocks"]

    content = []
    for block in blocks:
        if block["type"] == 0:  # Text block
            # Extract text from spans
            text_parts = []
            for line in block["lines"]:
                line_text = ""
                for span in line["spans"]:
                    line_text += span["text"]
                text_parts.append(line_text)

            text = "\n".join(text_parts)
            if text.strip():
                content.append(
                    {
                        "type": "text",
                        "text": text,
                        "y": block["bbox"][1],  # Top Y coordinate
                        "bbox": block["bbox"],
                    }
                )
        elif block["type"] == 1:  # Image block
            content.append(
                {
                    "type": "image_placeholder",
                    "y": block["bbox"][1],
                    "bbox": block["bbox"],
                }
            )

    # Sort by Y coordinate for natural reading order
    content.sort(key=lambda x: x["y"])

    return content


def extract_images_from_page(
    doc: pymupdf.Document,
    page_num: int,
    output_dir: Path | None = None,
    pdf_hash: str = "",
) -> list[dict[str, Any]]:
    """
    Extract images from a PDF page as PNG files saved to disk.

    Args:
        doc: PyMuPDF document object
        page_num: Page number (0-indexed)
        output_dir: Directory to save PNG files
        pdf_hash: Hash prefix for deterministic filenames

    Returns:
        List of image dicts with width, height, format, path, size_bytes
    """
    page = doc[page_num]
    images = []

    image_list = page.get_images(full=True)

    for img_index, img_info in enumerate(image_list):
        xref = img_info[0]

        try:
            # Extract image as Pixmap
            pix = pymupdf.Pixmap(doc, xref)

            # Handle CMYK images
            if pix.n - pix.alpha > 3:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)

            # Determine color format
            if pix.n == 1:
                color_format = "grayscale"
            elif pix.n == 3:
                color_format = "rgb"
            elif pix.n == 4:
                color_format = "rgba"
            else:
                color_format = "unknown"

            # Save to disk
            assert output_dir is not None
            file_name = f"{pdf_hash}_p{page_num}_i{img_index}.png"
            file_path = output_dir / file_name
            try:
                pix.save(str(file_path))
                os.chmod(str(file_path), 0o600)
            except Exception as e:
                try:
                    file_path.unlink(missing_ok=True)
                except Exception:
                    pass
                logger.warning(
                    "Failed to save image %d from page %d: %s",
                    img_index,
                    page_num,
                    e,
                )
                continue

            images.append(
                {
                    "page": page_num + 1,  # 1-indexed for output
                    "index": img_index,
                    "width": pix.width,
                    "height": pix.height,
                    "format": color_format,
                    "path": str(file_path),
                    "size_bytes": file_path.stat().st_size,
                }
            )

        except (ValueError, RuntimeError, KeyError) as e:
            # Skip problematic images but log the issue
            logger.warning(
                "Failed to extract image %d from page %d: %s", img_index, page_num, e
            )
            continue

    return images


def render_page_as_png(
    doc: pymupdf.Document,
    page_num: int,
    output_dir: Path,
    pdf_hash: str,
    dpi: int = 200,
) -> dict[str, Any]:
    """
    Render a PDF page as a PNG file.

    Args:
        doc: PyMuPDF document object
        page_num: Page number (0-indexed)
        output_dir: Directory to save the PNG
        pdf_hash: Hash prefix for deterministic filenames
        dpi: Render resolution (default 200)

    Returns:
        Dict with file_path_on_disk, size_bytes, width, height
    """
    page = doc[page_num]
    pix = page.get_pixmap(dpi=dpi)

    file_name = f"{pdf_hash}_p{page_num}_render_{dpi}dpi.png"
    file_path = output_dir / file_name
    try:
        pix.save(str(file_path))
        os.chmod(str(file_path), 0o600)
    except Exception as e:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass
        logger.warning("Failed to save render for page %d: %s", page_num, e)
        raise

    return {
        "file_path_on_disk": str(file_path),
        "size_bytes": file_path.stat().st_size,
        "width": pix.width,
        "height": pix.height,
    }


def check_tesseract_available() -> None:
    """
    Verify Tesseract binary is on PATH.

    Raises:
        RuntimeError: If tesseract binary is not found or returns non-zero.
    """
    import subprocess

    try:
        subprocess.run(
            ["tesseract", "--version"],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(
            "Tesseract not found. Install with: "
            "brew install tesseract (macOS) / "
            "apt install tesseract-ocr (Linux). "
            "See https://tesseract-ocr.github.io/tessdoc/Installation.html. "
            "If OCR returns empty for a page with visible text, also verify "
            "the language pack: tesseract --list-langs"
        ) from exc


def ocr_page(
    doc: pymupdf.Document,
    page_num: int,
    lang: str = "eng",
    dpi: int = 300,
) -> str:
    """
    OCR a PDF page using PyMuPDF's built-in Tesseract binding.

    Args:
        doc: PyMuPDF document object
        page_num: Page number (0-indexed)
        lang: Tesseract language code (default 'eng')
        dpi: Internal render DPI for OCR (fixed at 300 for v1; not user-configurable
             to keep the surface minimal — expose as parameter in a future release
             if user feedback demands finer control)

    Returns:
        Extracted text string (empty string if OCR produces nothing)
    """
    page = doc[page_num]
    textpage = page.get_textpage_ocr(language=lang, dpi=dpi)
    return str(page.get_text(textpage=textpage))


def _ocr_page_worker(
    args: tuple[str, int, str, int],
) -> tuple[int, "str | PageError"]:
    """Picklable OCR worker for ProcessPoolExecutor.

    Opens its OWN Document (PyMuPDF documents are not shareable across
    processes) and isolates per-page failure as a PageError so one bad page
    never crashes the batch. Lives in extractor.py (not server.py) so spawn
    re-imports only PyMuPDF, never FastMCP.
    """
    path, page_num, lang, dpi = args
    try:
        doc = pymupdf.open(path)
        try:
            return page_num, ocr_page(doc, page_num, lang=lang, dpi=dpi)
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 - deliberate per-page isolation
        return page_num, PageError(repr(exc))


def _render_page_worker(
    args: tuple[str, int, str, str, int],
) -> tuple[int, "dict[str, Any] | PageError"]:
    """Picklable render worker for ProcessPoolExecutor.

    Opens its own Document and writes the PNG to disk (filenames are
    deterministic from pdf_hash+page+dpi, so concurrent workers never collide).
    Returns the render_info dict; the parent records SQLite metadata.
    """
    path, page_num, out_dir, pdf_hash, dpi = args
    try:
        doc = pymupdf.open(path)
        try:
            info = render_page_as_png(doc, page_num, Path(out_dir), pdf_hash, dpi)
            return page_num, info
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 - deliberate per-page isolation
        return page_num, PageError(repr(exc))


def extract_tables_from_page(page: Any) -> list[dict[str, Any]]:
    """
    Extract tables from a PDF page using PyMuPDF's table finder.

    Requires visible line borders to detect table structure.
    Pages without detectable tables return an empty list.

    Args:
        page: PyMuPDF page object

    Returns:
        List of table dicts, each with:
        - index: 0-based table index on this page
        - bbox: [x0, y0, x1, y1] bounding box
        - row_count: total rows including header (equals 1 + len(rows))
        - col_count: number of columns
        - header: list of header cell strings (first row)
        - rows: list of data rows (excludes header); each row is a list of cell strings
    """
    tables = []
    try:
        found = page.find_tables()
        for i, table in enumerate(found.tables):
            extracted = table.extract()
            if not extracted:
                continue
            header = [str(cell) if cell is not None else "" for cell in extracted[0]]
            rows = [
                [str(cell) if cell is not None else "" for cell in row]
                for row in extracted[1:]
            ]
            tables.append(
                {
                    "index": i,
                    "bbox": list(table.bbox),
                    "row_count": len(extracted),
                    "col_count": len(extracted[0]),
                    "header": header,
                    "rows": rows,
                }
            )
    except Exception as e:
        logger.warning("Failed to extract tables from page: %s", e)
    return tables


def extract_metadata(doc: pymupdf.Document) -> dict[str, Any]:
    """
    Extract metadata from PDF document.

    Args:
        doc: PyMuPDF document object

    Returns:
        Metadata dict with author, title, subject, etc.
    """
    meta = doc.metadata or {}

    return {
        "title": meta.get("title", ""),
        "author": meta.get("author", ""),
        "subject": meta.get("subject", ""),
        "keywords": meta.get("keywords", ""),
        "creator": meta.get("creator", ""),
        "producer": meta.get("producer", ""),
        "creation_date": meta.get("creationDate", ""),
        "modification_date": meta.get("modDate", ""),
        "format": meta.get("format", ""),
        "encryption": meta.get("encryption", ""),
    }


def extract_toc(doc: pymupdf.Document) -> list[dict[str, Any]]:
    """
    Extract table of contents from PDF document.

    Args:
        doc: PyMuPDF document object

    Returns:
        List of TOC entries with level, title, page
    """
    toc = doc.get_toc()

    return [
        {
            "level": entry[0],
            "title": entry[1],
            "page": entry[2],
        }
        for entry in toc
    ]


def estimate_tokens(text: str) -> int:
    """
    Estimate token count for text (rough approximation).

    Uses ~4 characters per token as rough estimate.

    Args:
        text: Input text

    Returns:
        Estimated token count
    """
    return len(text) // 4


def chunk_text(
    text: str, max_tokens: int = 4000, overlap_tokens: int = 200
) -> list[dict[str, Any]]:
    """
    Split text into chunks with overlap.

    Args:
        text: Input text
        max_tokens: Maximum tokens per chunk
        overlap_tokens: Overlap tokens between chunks

    Returns:
        List of chunk dicts with text, start_char, end_char, estimated_tokens
    """
    max_chars = max_tokens * 4
    overlap_chars = overlap_tokens * 4

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(text):
        end = min(start + max_chars, len(text))

        # Try to break at sentence boundary
        if end < len(text):
            # Look for sentence end (.!?) followed by space or newline
            search_start = max(start + max_chars - 500, start)
            last_sentence = -1

            for i in range(end - 1, search_start, -1):
                if text[i] in ".!?" and (i + 1 >= len(text) or text[i + 1] in " \n\t"):
                    last_sentence = i + 1
                    break

            if last_sentence > start:
                end = last_sentence

        chunk_text = text[start:end]

        chunks.append(
            {
                "chunk_index": chunk_index,
                "text": chunk_text,
                "start_char": start,
                "end_char": end,
                "estimated_tokens": estimate_tokens(chunk_text),
            }
        )

        chunk_index += 1
        start = end - overlap_chars if end < len(text) else end

    return chunks
