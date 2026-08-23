"""Versioning of the table cache.

This file used to test a subprocess-isolation mechanism. That mechanism
existed because importing ``pymupdf4llm`` activated ``pymupdf.layout``
and irreversibly corrupted PyMuPDF's ``find_tables`` cell text
process-wide, detaching decimal points so a datasheet's ``4.5`` came
back as ``45\n.``. Table extraction therefore had to run in an
interpreter that had never imported it.

Both halves of that premise are gone: detection runs on pdfplumber, and
``pymupdf4llm`` is not a dependency of this project at all. The
corruption tests here had already degraded to skips because they guarded
their own premise, and the isolation hop was removed on 2026-08-23.

What survives is the part that still protects users: cached rows written
during the corrupt era must never be served. A cache from then holds
wrong numbers, and wrong numbers presented as table cells are worse than
no cells, so the version guard drops them rather than migrating them.
The decimal fixture is kept for the same reason it was introduced: a
fixture using integers cannot detect a detached decimal point.
"""

import sqlite3
import tempfile

import pymupdf
import pytest

from pdf_mcp.cache import PDFCache, TABLE_EXTRACTION_VERSION
from tests.tmpfiles import unlink_quietly


@pytest.fixture
def decimal_table_pdf():
    """A bordered table whose cells carry decimal values.

    Decimals are load-bearing: the corruption detaches the decimal point,
    so a fixture using integers (like `sample_pdf_with_table`) cannot
    detect it. That gap is why this shipped.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        # Close the handle before anything writes to this path: Windows
        # refuses to write or replace a file that is still open, which
        # turned into 639 errors the first time CI ran there. delete=False
        # means closing early does not remove the file.
        f.close()
        doc = pymupdf.open()
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(50, 50, 250, 150), color=(0, 0, 0))
        page.draw_line(pymupdf.Point(150, 50), pymupdf.Point(150, 150), color=(0, 0, 0))
        page.draw_line(pymupdf.Point(50, 83), pymupdf.Point(250, 83), color=(0, 0, 0))
        page.draw_line(pymupdf.Point(50, 116), pymupdf.Point(250, 116), color=(0, 0, 0))
        page.insert_text((55, 75), "Parameter")
        page.insert_text((155, 75), "Value")
        page.insert_text((55, 108), "Supply Voltage")
        page.insert_text((155, 108), "4.5")
        page.insert_text((55, 141), "Drift")
        page.insert_text((155, 141), "0.25")
        doc.save(f.name)
        doc.close()
    yield f.name
    unlink_quietly(f.name)


def _cells(tables):
    return [c for t in tables for row in t["rows"] for c in row if c]


def test_cached_rows_from_the_corrupt_era_are_ignored(tmp_path, decimal_table_pdf):
    """A row at an older extraction version must not be served."""
    cache = PDFCache(cache_dir=tmp_path)
    cache.save_page_tables(decimal_table_pdf, 0, [{"rows": [["ok"]]}])
    assert cache.get_page_tables(decimal_table_pdf, 0) is not None
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute(
            "UPDATE page_tables SET extraction_version = ?",
            (TABLE_EXTRACTION_VERSION - 1,),
        )
    assert cache.get_page_tables(decimal_table_pdf, 0) is None


def test_migration_drops_page_tables_lacking_a_version_column(tmp_path):
    """A pre-fix cache holds corrupt cells, so it is dropped, not migrated."""
    db = tmp_path / "cache.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE page_tables (file_path TEXT NOT NULL,"
            " page_num INTEGER NOT NULL, file_mtime REAL NOT NULL,"
            " data TEXT NOT NULL, PRIMARY KEY (file_path, page_num))"
        )
        conn.execute(
            "INSERT INTO page_tables VALUES (?, ?, ?, ?)",
            ("/x.pdf", 0, 1.0, '[{"rows": [["45\\n."]]}]'),
        )
    PDFCache(cache_dir=tmp_path)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM page_tables").fetchone()[0]
    assert rows == 0
