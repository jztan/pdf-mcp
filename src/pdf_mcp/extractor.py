"""
PDF extraction utilities using PyMuPDF.
"""

import base64
import logging
import re
from typing import Any

import pymupdf

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
        # Get text with position information
        blocks = page.get_text("blocks", sort=True)

        # blocks format: (x0, y0, x1, y1, "text", block_no, block_type)
        # block_type: 0 = text, 1 = image
        text_blocks = [block[4] for block in blocks if block[6] == 0]

        return "\n\n".join(text_blocks)
    else:
        return str(page.get_text())


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
    doc: pymupdf.Document, page_num: int
) -> list[dict[str, Any]]:
    """
    Extract images from a PDF page as base64-encoded PNG.

    Args:
        doc: PyMuPDF document object
        page_num: Page number (0-indexed)

    Returns:
        List of image dicts with width, height, format, data (base64)
    """
    page = doc[page_num]
    images = []

    image_list = page.get_images(full=True)

    for img_index, img_info in enumerate(image_list):
        xref = img_info[0]

        try:
            # Extract image
            base_image = doc.extract_image(xref)

            if base_image:
                # Convert to PNG if needed for consistency
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

                # Convert to PNG bytes
                png_bytes = pix.tobytes("png")

                images.append(
                    {
                        "page": page_num + 1,  # 1-indexed for output
                        "index": img_index,
                        "width": pix.width,
                        "height": pix.height,
                        "format": color_format,
                        "data": base64.b64encode(png_bytes).decode("ascii"),
                    }
                )

        except (ValueError, RuntimeError, KeyError) as e:
            # Skip problematic images but log the issue
            logger.warning(
                "Failed to extract image %d from page %d: %s", img_index, page_num, e
            )
            continue

    return images


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
