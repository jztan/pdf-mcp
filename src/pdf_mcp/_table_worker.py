"""Standalone table-extraction worker. Run as ``python -m pdf_mcp._table_worker``.

Reads ``{"path": str, "pages": [int]}`` as JSON on stdin and writes
``{"tables": {page: [...]}, "errors": {page: str}}`` as JSON on stdout.

Historical note: this module exists because table extraction once had to
run in an interpreter that had never imported ``pymupdf4llm`` (that
import corrupted PyMuPDF's ``find_tables`` process-wide). Detection now
runs on pdfplumber, which has no such corruption, but the worker and its
stdout-JSON contract are kept so the server's versioned cache path and
its callers stay unchanged; the stdout-noise guard in ``main`` still
protects the JSON channel from anything a library prints.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from typing import Any


from .extractor import extract_tables_from_page


def extract(path: str, pages: list[int]) -> dict[str, Any]:
    """Extract tables for `pages` (0-indexed) of the document at `path`.

    Runs on the pdfplumber backend. Per-page failure is reported in
    `errors` rather than raising, so one unreadable page cannot cost the
    whole batch. A page that fails is absent from `tables`: the caller
    must not read "no entry" as "no tables on this page", or it would
    cache a false empty.
    """
    from .backend.tables import open_table_page

    tables: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for page_num in pages:
        try:
            tables[str(page_num)] = extract_tables_from_page(
                open_table_page(path, page_num)
            )
        except Exception as exc:  # noqa: BLE001 - per-page isolation
            errors[str(page_num)] = repr(exc)
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
