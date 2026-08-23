"""Open a PDF from bytes, distinguishing "password-locked" from "corrupt".

url_fetcher validates downloads before caching them, and its contract
(issue #19) is that a password-protected PDF PASSES validation: opening
proved a structurally valid PDF even though the page tree is unreadable.
PyMuPDF expressed that as needs_pass on an open document; pdfium instead
raises at open time, with a distinct error for the password case, so the
distinction is rebuilt here from the error kind.
"""

from __future__ import annotations

from typing import Any

import pypdfium2 as pdfium


def open_pdf_bytes(content: bytes) -> tuple[Any, bool]:
    """Return (document, needs_pass). Raises on genuinely corrupt input.

    When needs_pass is True the document is None: pdfium cannot open it
    without the password, and the caller treats it as valid-but-locked.
    """
    try:
        return pdfium.PdfDocument(content), False
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            return None, True
        raise
