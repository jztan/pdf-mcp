"""Single place where a PDF is opened.

Exists so the permissive backend can be exercised end to end before it
becomes the default. ``PDF_MCP_BACKEND=pdfium`` routes every path-based
open through ``backend.page``; anything else keeps PyMuPDF.

The flag is read per call rather than cached at import, because the
benchmarks set it in the environment of a subprocess that may already
have imported this module.
"""

from __future__ import annotations

import os
from typing import Any


def use_backend() -> bool:
    return os.environ.get("PDF_MCP_BACKEND") == "pdfium"


def open_pdf(path: str) -> Any:
    """Open a PDF by path, honouring PDF_MCP_BACKEND."""
    if use_backend():
        from .backend.page import open_document

        return open_document(str(path))

    import pymupdf

    return pymupdf.open(str(path))
