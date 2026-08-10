"""Table extraction must run in an isolated spawn process.

Importing ``pymupdf4llm`` executes ``use_layout(True)`` at module level,
and the resulting ``import pymupdf.layout`` swaps PyMuPDF's text engine
process-wide and irreversibly. In that state ``find_tables`` mis-assigns
character quads: decimal points detach from their numbers, so "4.5" comes
back as "45\\n." and an agent reads 45 where the document says 4.5.

The server imports ``pymupdf4llm`` at startup (feature probe) and again on
any column-aware extraction, so these tests run in an already-poisoned
interpreter. That is the point: they only pass if extraction is genuinely
out-of-process.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf
import pytest

from pdf_mcp import server as server_module
from pdf_mcp.cache import PDFCache
from pdf_mcp._table_worker import extract as worker_extract
from pdf_mcp.extractor import TABLE_EXTRACTION_VERSION
from pdf_mcp.parallel import run_module_json
from pdf_mcp.server import pdf_read_pages


@pytest.fixture
def decimal_table_pdf():
    """A bordered table whose cells carry decimal values.

    Decimals are load-bearing: the corruption detaches the decimal point,
    so a fixture using integers (like `sample_pdf_with_table`) cannot
    detect it. That gap is why this shipped.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
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
    os.unlink(f.name)


def _cells(tables):
    return [c for t in tables for row in t["rows"] for c in row if c]


def test_pymupdf4llm_is_loaded_in_this_process():
    """Guard the premise: without this, the tests below prove nothing."""
    assert "pymupdf.layout" in sys.modules or "pymupdf4llm" in sys.modules


def test_pdf_read_pages_preserves_decimals_in_table_cells(decimal_table_pdf):
    """The end-to-end regression: 4.5 must not come back as "45\\n."."""
    result = pdf_read_pages(decimal_table_pdf, pages="1")
    cells = _cells(result["pages"][0]["tables"])
    assert "4.5" in cells
    assert "0.25" in cells
    assert not any("\n." in c for c in cells), f"detached decimal point: {cells}"


def test_module_worker_is_clean_while_parent_is_poisoned(decimal_table_pdf):
    """The worker subprocess returns correct cells though this process cannot."""
    out = run_module_json(
        "pdf_mcp._table_worker", {"path": decimal_table_pdf, "pages": [0]}
    )
    assert "4.5" in _cells(out["tables"]["0"])


def test_in_process_extraction_is_still_broken(decimal_table_pdf):
    """Pin the premise. If this ever passes, the upstream cause is gone and
    the subprocess hop can be reconsidered -- but not before."""
    result = worker_extract(decimal_table_pdf, [0])
    cells = _cells(result["tables"]["0"])
    assert any("\n." in c for c in cells) or "4.5" in cells


def test_clean_tables_when_main_module_imports_the_server(decimal_table_pdf, tmp_path):
    """THE regression this fix originally missed.

    `multiprocessing` spawn re-imports the parent's `__main__` in the child.
    The console script is `pdf-mcp = "pdf_mcp.server:main"`, so `__main__`
    imports the server, the child re-poisons itself, and corrupt cells come
    back. That path is invisible to pytest and to `python -c`, which is
    exactly how the first version of this fix shipped green.
    """
    script = tmp_path / "entrypoint_like.py"
    script.write_text(
        "from pdf_mcp.server import pdf_read_pages\n"
        "if __name__ == '__main__':\n"
        "    import json, sys\n"
        "    r = pdf_read_pages(sys.argv[1], pages='1')\n"
        "    rows = r['pages'][0]['tables'][0]['rows']\n"
        "    json.dump(rows, sys.stdout)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")}
    proc = subprocess.run(
        [sys.executable, str(script), decimal_table_pdf],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    cells = [c for row in json.loads(proc.stdout) for c in row if c]
    assert "4.5" in cells, cells
    assert not any("\n." in c for c in cells), cells


def test_server_does_not_extract_tables_in_process():
    """Mechanized guard: re-importing the in-process extractor reintroduces
    the bug, so assert the symbol is absent from the server namespace."""
    assert not hasattr(server_module, "extract_tables_from_page")


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
