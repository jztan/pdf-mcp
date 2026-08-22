"""Standalone table-extraction worker. Run as ``python -m pdf_mcp._table_worker``.

Reads ``{"path": str, "pages": [int]}`` as JSON on stdin and writes
``{"tables": {page: [...]}, "errors": {page: str}}`` as JSON on stdout.

This module exists because table extraction must happen in an interpreter
that has never imported ``pymupdf4llm``. Importing it runs
``use_layout(True)`` at module level, and the resulting
``import pymupdf.layout`` swaps PyMuPDF's text engine process-wide and
irreversibly, detaching decimal points in table cells ("4.5" -> "45\\n.").

``multiprocessing`` with ``spawn`` is NOT sufficient here. Spawn re-imports
the parent's ``__main__`` module in the child, so whenever ``__main__``
imports ``pdf_mcp.server`` -- which the console-script entry point
(``pdf-mcp = "pdf_mcp.server:main"``) does -- the child poisons itself
before doing any work. Running ``-m`` makes THIS module ``__main__``, which
imports only PyMuPDF and the extractor, so the child is clean no matter how
the parent was started.

Keep this module's imports minimal. Anything it pulls in transitively must
stay free of ``pymupdf4llm``; ``pdf_mcp/__init__`` is deliberately lazy
about ``mcp`` for the same reason.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from typing import Any


from .docopen import open_pdf
from .extractor import extract_tables_from_page


def extract(path: str, pages: list[int]) -> dict[str, Any]:
    """Extract tables for `pages` (0-indexed) of the document at `path`.

    Per-page failure is reported in `errors` rather than raising, so one
    unreadable page cannot cost the whole batch. A page that fails is
    absent from `tables`: the caller must not read "no entry" as "no
    tables on this page", or it would cache a false empty.
    """
    tables: dict[str, Any] = {}
    errors: dict[str, str] = {}

    if os.environ.get("PDF_MCP_TABLE_BACKEND") == "pdfplumber":
        # Migration switch, off by default. Routed here rather than only in
        # extractor._extract_tables_worker because THIS is the live path:
        # the server always reaches table extraction through this module.
        from .backend.tables import open_table_page

        for page_num in pages:
            try:
                tables[str(page_num)] = extract_tables_from_page(
                    open_table_page(path, page_num)
                )
            except Exception as exc:  # noqa: BLE001 - per-page isolation
                errors[str(page_num)] = repr(exc)
        return {"tables": tables, "errors": errors}

    doc = open_pdf(path)
    try:
        for page_num in pages:
            try:
                tables[str(page_num)] = extract_tables_from_page(doc[page_num])
            except Exception as exc:  # noqa: BLE001 - per-page isolation
                errors[str(page_num)] = repr(exc)
    finally:
        doc.close()
    return {"tables": tables, "errors": errors}


def main() -> int:
    """Exchange JSON over stdio.

    Extraction runs with stdout redirected because PyMuPDF writes advisory
    text there. With ``pymupdf_layout`` absent it emits "Consider using the
    pymupdf_layout package for a greatly improved page layout analysis.",
    which landed ahead of the payload and made the parent's ``json.loads``
    fail with "Expecting value: line 1 column 1". The dependency was
    masking that; stdout is this worker's data channel and nothing else may
    write to it.
    """
    request = json.load(sys.stdin)
    noise = io.StringIO()
    with contextlib.redirect_stdout(noise):
        result = extract(request["path"], request["pages"])
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
