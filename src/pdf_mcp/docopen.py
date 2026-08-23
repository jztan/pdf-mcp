"""Single place where a PDF is opened.

The permissive backend (pypdfium2 + pdfplumber + pypdf) is the ONLY
runtime engine: pymupdf is a dev-only dependency, present for the test
fixtures and the differential benchmarks, and a published wheel carries
no AGPL code. ``PDF_MCP_BACKEND=pymupdf`` switches back for A/B
measurement in a dev environment where pymupdf is installed; it is not a
supported runtime mode.

The flag is read per call rather than cached at import, because the
benchmarks set it in the environment of a subprocess that may already
have imported this module.
"""

from __future__ import annotations

import os
from typing import Any


def use_backend() -> bool:
    return os.environ.get("PDF_MCP_BACKEND") != "pymupdf"


def open_pdf(path: str) -> Any:
    """Open a PDF by path, honouring PDF_MCP_BACKEND."""
    if use_backend():
        from .backend.page import open_document

        return open_document(str(path))

    import pymupdf

    return pymupdf.open(str(path))
