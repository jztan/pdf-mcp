"""Open a PDF from bytes, distinguishing "password-locked" from "corrupt".

url_fetcher validates downloads before caching them, and its contract
(issue #19) is that a password-protected PDF PASSES validation: opening
proved a structurally valid PDF even though the page tree is unreadable.
PyMuPDF expressed that as needs_pass on an open document; pdfium instead
raises at open time, with a distinct error for the password case, so the
distinction is rebuilt here from the error kind.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
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


#: Per-process memo of files known NOT to need a password, keyed on file
#: identity (abspath, mtime_ns, size). Every single-file tool call and every
#: corpus resolution runs the check, so a repeat call on a known file must not
#: reopen it. Locked and failed verdicts are never stored: a locked file is
#: re-checked on each call, and a corrupt file falls through to the normal
#: open path, which produces its own error.
_LOCK_MEMO_MAX = 256
_NOT_LOCKED_MEMO: "OrderedDict[tuple[str, int, int], None]" = OrderedDict()
_LOCK_MEMO_LOCK = threading.Lock()


def clear_lock_memo() -> None:
    with _LOCK_MEMO_LOCK:
        _NOT_LOCKED_MEMO.clear()


def is_password_locked(path: str) -> bool:
    """True when the PDF at `path` cannot be opened without a user password.

    Owner-password-only PDFs open without one and return False. Any other
    open failure (missing, corrupt) also returns False, so the caller's
    normal path reports it unchanged.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    with _LOCK_MEMO_LOCK:
        if key in _NOT_LOCKED_MEMO:
            _NOT_LOCKED_MEMO.move_to_end(key)
            return False
    try:
        doc = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        return "password" in str(exc).lower()
    except Exception:
        return False
    doc.close()
    with _LOCK_MEMO_LOCK:
        _NOT_LOCKED_MEMO[key] = None
        _NOT_LOCKED_MEMO.move_to_end(key)
        while len(_NOT_LOCKED_MEMO) > _LOCK_MEMO_MAX:
            _NOT_LOCKED_MEMO.popitem(last=False)
    return False
