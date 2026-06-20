"""
SQLite-based cache for PDF data persistence across MCP server restarts.
"""

import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pdf_mcp.embedder import DEFAULT_MODEL
from pdf_mcp.section_detector import Section

# FTS5 virtual table schema for full-text search with Porter stemmer.
# Must be created in a separate conn.execute() call (not inside executescript)
# so that FTS5 unavailability can be caught in isolation.
_FTS5_TABLE_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS pdf_search_fts USING fts5("
    "file_path UNINDEXED, "
    "page_num UNINDEXED, "
    "text, "
    "tokenize='porter unicode61'"
    ")"
)

_FTS5_SECTION_TABLE_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS pdf_section_fts USING fts5("
    "file_path UNINDEXED, "
    "section_id UNINDEXED, "
    "title, "
    "text, "
    "start_page UNINDEXED, "
    "end_page UNINDEXED, "
    "title_source UNINDEXED, "
    "tokenize='porter unicode61'"
    ")"
)

_FTS5_CJK_TABLE_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS pdf_search_fts_cjk USING fts5("
    "file_path UNINDEXED, "
    "page_num UNINDEXED, "
    "text, "
    "tokenize='unicode61'"
    ")"
)

_FTS5_CJK_SECTION_TABLE_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS pdf_section_fts_cjk USING fts5("
    "file_path UNINDEXED, "
    "section_id UNINDEXED, "
    "title, "
    "text, "
    "start_page UNINDEXED, "
    "end_page UNINDEXED, "
    "title_source UNINDEXED, "
    "tokenize='unicode61'"
    ")"
)


# Bump when text-extraction logic changes so cached text + everything derived
# from it (embeddings, FTS indexes) is dropped and rebuilt. v1: column-aware
# reading order for multi-column PDFs. v2: suppress the column path on sparse
# grids (e.g. author/affiliation blocks on academic title pages) that v1
# mis-read column-major — drops v1's scrambled title-page text/embeddings/FTS.
_EXTRACTION_VERSION = 5  # 5: dense-vertical article segmentation + mojibake filter

_FTS_TOKEN_STRIP = re.compile(r'["()*:^]')
_NO_MATCH_SENTINEL = '"__pdfmcp_no_match_sentinel__"'

# Unicode blocks treated as CJK for character-split FTS tokenization. Covers
# the high-frequency core; rarer blocks (CJK Ext-B+, Hangul Jamo) intentionally
# fall through to whole-token (old behavior) — a documented gap, not a surprise.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7AF),  # Hangul Syllables
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFF00, 0xFFEF),  # Halfwidth/Fullwidth Forms
)


def _is_cjk_char(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _CJK_RANGES)


def _contains_cjk(text: str) -> bool:
    """True if text contains any character in _CJK_RANGES."""
    return any(_is_cjk_char(ch) for ch in text)


def _cjk_split(text: str) -> str:
    """Insert a space around every CJK codepoint; other runs pass through.

    Defines the CJK/Latin token boundary for BOTH the write path and the
    query escaper, so the two token streams cannot diverge. Idempotent.
    """
    out: list[str] = []
    for ch in text:
        if _is_cjk_char(ch):
            out.append(" ")
            out.append(ch)
            out.append(" ")
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def _escape_fts5_query(query: str) -> str:
    """
    Escape a user-supplied query for FTS5 MATCH expressions.

    Tokenises the query on whitespace, strips FTS5 reserved characters
    from each token, wraps each non-empty token in double-quotes, and
    joins with spaces. FTS5 treats space-separated quoted tokens as an
    implicit AND, so all words must appear on the same page; BM25 then
    ranks pages by combined token frequency. Word order does not matter.

    Returns a sentinel token that matches nothing when the query has no
    extractable tokens (e.g. only punctuation).
    """
    tokens: list[str] = []
    for raw in query.split():
        cleaned = _FTS_TOKEN_STRIP.sub("", raw)
        if cleaned:
            tokens.append(f'"{cleaned}"')
    if not tokens:
        return _NO_MATCH_SENTINEL
    return " ".join(tokens)


def _get_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Return column names for a table, or empty set if the table does not exist."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


class PDFCache:
    """
    SQLite-based cache for PDF metadata and page text.

    Persists data to disk so it survives MCP server process restarts.
    Uses file modification time for cache invalidation.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        ttl_hours: int = 24,
        images_dir: Path | None = None,
    ):
        """
        Initialize the cache.

        Args:
            cache_dir: Directory to store cache database. Defaults to ~/.cache/pdf-mcp
            ttl_hours: Time-to-live for cache entries in hours
            images_dir: Directory to store extracted images.
                Defaults to cache_dir/images
        """
        if cache_dir is None:
            cache_dir = Path.home() / ".cache" / "pdf-mcp"

        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Tighten perms on the cache dir itself (images/renders subdirs
        # already get 0o700 below). Closes a multi-user info-leak gap
        # where cached PDF text was readable via the user's umask.
        os.chmod(str(self.cache_dir), 0o700)
        self.db_path = self.cache_dir / "cache.db"
        self.ttl_hours = ttl_hours
        self.images_dir = images_dir or (self.cache_dir / "images")
        self.images_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.images_dir), 0o700)
        self.renders_dir = self.cache_dir / "renders"
        self.renders_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.renders_dir), 0o700)
        self._init_db()

    def _init_db(self) -> None:
        """
        Initialize database schema.

        Side effect: sets self.fts_available (bool) indicating whether
        the SQLite build supports FTS5 virtual tables.
        """
        with sqlite3.connect(self.db_path) as conn:
            # Extraction-logic version: drop cached text and all text-derived
            # tables when the extraction algorithm changes, forcing re-extract.
            # Only runs when the DB is non-empty (user_version=0 on a brand-new
            # DB is indistinguishable from a pre-v1 cache; guard on the
            # presence of the page_text table to avoid wiping a fresh init).
            (extraction_version,) = conn.execute("PRAGMA user_version").fetchone()
            has_page_text = bool(_get_columns(conn, "page_text"))
            if extraction_version < _EXTRACTION_VERSION and has_page_text:
                conn.execute("DROP TABLE IF EXISTS page_text")
                conn.execute("DROP TABLE IF EXISTS page_embeddings")
                conn.execute("DROP TABLE IF EXISTS pdf_search_fts")
                conn.execute("DROP TABLE IF EXISTS pdf_section_fts")
            if extraction_version < _EXTRACTION_VERSION:
                conn.execute(f"PRAGMA user_version = {_EXTRACTION_VERSION}")

            # page_images: old schema stored binary data instead of file path
            cols = _get_columns(conn, "page_images")
            if "data" in cols or (cols and "file_path_on_disk" not in cols):
                conn.execute("DROP TABLE IF EXISTS page_images")

            # page_tables: introduced in v1.5.0 — older caches may lack 'data' column
            cols = _get_columns(conn, "page_tables")
            if cols and "data" not in cols:
                conn.execute("DROP TABLE IF EXISTS page_tables")

            # pdf_metadata: drop if missing any required column
            cols = _get_columns(conn, "pdf_metadata")
            if cols and not {"file_path", "page_count", "file_mtime"}.issubset(cols):
                conn.execute("DROP TABLE IF EXISTS pdf_metadata")

            # page_text: drop if missing any required column
            cols = _get_columns(conn, "page_text")
            if cols and not {"file_path", "page_num", "text"}.issubset(cols):
                conn.execute("DROP TABLE IF EXISTS page_text")

            # page_embeddings: only drop if schema is actually broken — preserve
            # existing embeddings (expensive to regenerate) whenever possible
            cols = _get_columns(conn, "page_embeddings")
            if cols and "embedding" not in cols:
                conn.execute("DROP TABLE IF EXISTS page_embeddings")

            # page_renders: drop if missing required columns
            cols = _get_columns(conn, "page_renders")
            if cols and not {
                "file_path",
                "page_num",
                "dpi",
                "file_path_on_disk",
            }.issubset(cols):
                conn.execute("DROP TABLE IF EXISTS page_renders")

            conn.executescript("""
                -- PDF metadata cache
                CREATE TABLE IF NOT EXISTS pdf_metadata (
                    file_path TEXT PRIMARY KEY,
                    file_mtime REAL NOT NULL,
                    file_size INTEGER NOT NULL,
                    page_count INTEGER NOT NULL,
                    metadata JSON,
                    toc JSON,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                -- Page text cache
                CREATE TABLE IF NOT EXISTS page_text (
                    file_path TEXT NOT NULL,
                    page_num INTEGER NOT NULL,
                    file_mtime REAL NOT NULL,
                    text TEXT NOT NULL,
                    text_length INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_path, page_num)
                );

                -- Page images cache (stores file paths)
                CREATE TABLE IF NOT EXISTS page_images (
                    file_path TEXT NOT NULL,
                    page_num INTEGER NOT NULL,
                    image_index INTEGER NOT NULL,
                    file_mtime REAL NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    format TEXT NOT NULL,
                    file_path_on_disk TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_path, page_num, image_index)
                );

                -- Indexes for faster lookups
                CREATE INDEX IF NOT EXISTS idx_page_text_path
                    ON page_text(file_path);
                CREATE INDEX IF NOT EXISTS idx_page_images_path
                    ON page_images(file_path);
                CREATE INDEX IF NOT EXISTS idx_metadata_accessed
                    ON pdf_metadata(accessed_at);

                -- Page tables cache
                CREATE TABLE IF NOT EXISTS page_tables (
                    file_path  TEXT    NOT NULL,
                    page_num   INTEGER NOT NULL,
                    file_mtime REAL    NOT NULL,
                    data       TEXT    NOT NULL,
                    PRIMARY KEY (file_path, page_num)
                );

                CREATE INDEX IF NOT EXISTS idx_page_tables_path
                    ON page_tables(file_path);

                -- Page embeddings cache (raw float32 BLOBs for semantic search)
                CREATE TABLE IF NOT EXISTS page_embeddings (
                    file_path   TEXT    NOT NULL,
                    page_num    INTEGER NOT NULL,
                    file_mtime  REAL    NOT NULL,
                    embedding   BLOB    NOT NULL,
                    model       TEXT    NOT NULL DEFAULT 'BAAI/bge-small-en-v1.5',
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_path, page_num)
                );

                CREATE INDEX IF NOT EXISTS idx_page_embeddings_path
                    ON page_embeddings(file_path);

                -- Section embeddings cache (Phase-1 validation shim;
                -- mirrors page_embeddings, keyed by section_id within a PDF).
                CREATE TABLE IF NOT EXISTS section_embeddings (
                    file_path   TEXT    NOT NULL,
                    section_id  INTEGER NOT NULL,
                    section_key TEXT    NOT NULL,
                    file_mtime  REAL    NOT NULL,
                    embedding   BLOB    NOT NULL,
                    model       TEXT    NOT NULL,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_path, section_id)
                );

                CREATE INDEX IF NOT EXISTS idx_section_embeddings_path
                    ON section_embeddings(file_path);

                -- Page render cache (full-page PNG renders)
                CREATE TABLE IF NOT EXISTS page_renders (
                    file_path          TEXT    NOT NULL,
                    page_num           INTEGER NOT NULL,
                    file_mtime         REAL    NOT NULL,
                    dpi                INTEGER NOT NULL,
                    file_path_on_disk  TEXT    NOT NULL,
                    size_bytes         INTEGER NOT NULL,
                    width              INTEGER NOT NULL,
                    height             INTEGER NOT NULL,
                    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_path, page_num, dpi)
                );

                CREATE INDEX IF NOT EXISTS idx_page_renders_path
                    ON page_renders(file_path);
            """)

            # page_text: add source column to existing tables (safe ALTER TABLE)
            cols = _get_columns(conn, "page_text")
            if cols and "source" not in cols:
                conn.execute(
                    "ALTER TABLE page_text ADD COLUMN source TEXT DEFAULT 'extracted'"
                )

            # pdf_metadata: add text_coverage_json column to existing tables
            cols = _get_columns(conn, "pdf_metadata")
            if cols and "text_coverage_json" not in cols:
                conn.execute(
                    "ALTER TABLE pdf_metadata"
                    " ADD COLUMN text_coverage_json TEXT DEFAULT NULL"
                )

            # page_embeddings: add model column to existing tables
            cols = _get_columns(conn, "page_embeddings")
            if cols and "model" not in cols:
                conn.execute(
                    f"ALTER TABLE page_embeddings"
                    f" ADD COLUMN model TEXT NOT NULL DEFAULT '{DEFAULT_MODEL}'"
                )

            # FTS5 virtual table must be in a separate execute() call so that
            # OperationalError from missing FTS5 support can be caught in isolation.
            try:
                conn.execute(_FTS5_TABLE_SCHEMA)
                self.fts_available = True
            except sqlite3.OperationalError:
                self.fts_available = False

            if self.fts_available:
                # Section FTS table: drop and recreate if the title_source
                # column is missing (pre-1.13 cache). FTS5 virtual tables
                # don't support ALTER ADD COLUMN, so a drop+recreate is
                # the only path. Sections are cheap to re-derive lazily on
                # the next section-mode search.
                section_cols = _get_columns(conn, "pdf_section_fts")
                if section_cols and "title_source" not in section_cols:
                    conn.execute("DROP TABLE IF EXISTS pdf_section_fts")
                try:
                    conn.execute(_FTS5_SECTION_TABLE_SCHEMA)
                except sqlite3.OperationalError:
                    # Section table failed but page table succeeded — unusual.
                    # Leave fts_available=True since page search still works.
                    pass

                try:
                    conn.execute(_FTS5_CJK_TABLE_SCHEMA)
                    conn.execute(_FTS5_CJK_SECTION_TABLE_SCHEMA)
                except sqlite3.OperationalError:
                    # CJK tables failed but porter tables succeeded — leave
                    # fts_available=True; CJK queries degrade to no-match.
                    pass

        self.clear_expired()

    def _get_file_info(self, path: str) -> tuple[float, int]:
        """Get file modification time and size."""
        stat = os.stat(path)
        return stat.st_mtime, stat.st_size

    def _is_cache_valid(self, path: str, cached_mtime: float) -> bool:
        """Check if cache entry is still valid based on file mtime."""
        try:
            current_mtime, _ = self._get_file_info(path)
            return current_mtime == cached_mtime
        except OSError:
            return False

    # ==================== Metadata Operations ====================

    def get_metadata(self, path: str) -> dict[str, Any] | None:
        """
        Get cached metadata for a PDF file.

        Args:
            path: Path to PDF file

        Returns:
            Cached metadata dict or None if not cached/invalid
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT file_mtime, file_size, page_count,
                   metadata, toc, text_coverage_json
                   FROM pdf_metadata WHERE file_path = ?""",
                (path,),
            ).fetchone()

            if row is None:
                return None

            # Validate cache
            if not self._is_cache_valid(path, row["file_mtime"]):
                self._invalidate_file(path)
                return None

            # Update access time
            conn.execute(
                "UPDATE pdf_metadata SET accessed_at = CURRENT_TIMESTAMP"
                " WHERE file_path = ?",
                (path,),
            )

            return {
                "file_path": path,
                "file_size": row["file_size"],
                "page_count": row["page_count"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
                "toc": json.loads(row["toc"]) if row["toc"] else [],
                "text_coverage": (
                    json.loads(row["text_coverage_json"])
                    if row["text_coverage_json"]
                    else None
                ),
            }

    def save_metadata(
        self,
        path: str,
        page_count: int,
        metadata: dict[str, Any],
        toc: list[Any],
        text_coverage: list[dict[str, Any]] | None = None,
    ) -> None:
        """Save PDF metadata to cache, including optional text_coverage."""
        mtime, size = self._get_file_info(path)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO pdf_metadata
                   (file_path, file_mtime, file_size,
                    page_count, metadata, toc,
                    text_coverage_json, accessed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    path,
                    mtime,
                    size,
                    page_count,
                    json.dumps(metadata),
                    json.dumps(toc),
                    json.dumps(text_coverage) if text_coverage is not None else None,
                ),
            )

    # ==================== Page Text Operations ====================

    def get_page_text(self, path: str, page_num: int) -> str | None:
        """
        Get cached text for a specific page.

        Args:
            path: Path to PDF file
            page_num: Page number (0-indexed)

        Returns:
            Cached text or None if not cached/invalid
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT text, file_mtime FROM page_text
                   WHERE file_path = ? AND page_num = ?""",
                (path, page_num),
            ).fetchone()

            if row is None:
                return None

            if not self._is_cache_valid(path, row[1]):
                return None

            return str(row[0])

    def get_pages_text(self, path: str, page_nums: list[int]) -> dict[int, str]:
        """
        Get cached text for multiple pages.

        Args:
            path: Path to PDF file
            page_nums: List of page numbers (0-indexed)

        Returns:
            Dict mapping page_num to text for cached pages
        """
        if not page_nums:
            return {}

        placeholders = ",".join("?" * len(page_nums))

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"""SELECT page_num, text, file_mtime
                    FROM page_text
                    WHERE file_path = ?
                    AND page_num IN ({placeholders})""",
                (path, *page_nums),
            ).fetchall()

            result = {}
            for page_num, text, mtime in rows:
                if self._is_cache_valid(path, mtime):
                    result[page_num] = text

            return result

    def save_page_text(
        self, path: str, page_num: int, text: str, source: str = "extracted"
    ) -> None:
        """Save page text to cache with optional source label ('extracted' or 'ocr')."""
        mtime, _ = self._get_file_info(path)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO page_text
                   (file_path, page_num, file_mtime,
                    text, text_length, source)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (path, page_num, mtime, text, len(text), source),
            )

            if self.fts_available:
                # DELETE + INSERT for de-duplication (FTS5 has no PRIMARY KEY)
                conn.execute(
                    "DELETE FROM pdf_search_fts"
                    " WHERE file_path = ? AND page_num = ?",
                    (path, page_num),
                )
                conn.execute(
                    "INSERT INTO pdf_search_fts (file_path, page_num, text)"
                    " VALUES (?, ?, ?)",
                    (path, page_num, text),
                )
                if _contains_cjk(text):
                    conn.execute(
                        "DELETE FROM pdf_search_fts_cjk"
                        " WHERE file_path = ? AND page_num = ?",
                        (path, page_num),
                    )
                    conn.execute(
                        "INSERT INTO pdf_search_fts_cjk"
                        " (file_path, page_num, text) VALUES (?, ?, ?)",
                        (path, page_num, _cjk_split(text)),
                    )

    def get_page_source(self, path: str, page_num: int) -> str | None:
        """Return 'extracted', 'ocr', or None (page not cached)."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT source, file_mtime FROM page_text"
                " WHERE file_path = ? AND page_num = ?",
                (path, page_num),
            ).fetchone()
            if row is None:
                return None
            if not self._is_cache_valid(path, row[1]):
                return None
            return str(row[0]) if row[0] else "extracted"

    def get_pages_source(self, path: str, page_nums: list[int]) -> dict[int, str]:
        """Bulk lookup of source for multiple pages. Missing/stale pages omitted."""
        if not page_nums:
            return {}
        placeholders = ",".join("?" * len(page_nums))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT page_num, source, file_mtime FROM page_text"
                f" WHERE file_path = ? AND page_num IN ({placeholders})",
                (path, *page_nums),
            ).fetchall()
        return {
            int(page_num): (str(source) if source else "extracted")
            for page_num, source, mtime in rows
            if self._is_cache_valid(path, mtime)
        }

    def save_pages_text(self, path: str, pages: dict[int, str]) -> None:
        """
        Save multiple page texts to cache.

        Args:
            path: Path to PDF file
            pages: Dict mapping page_num to text
        """
        if not pages:
            return

        mtime, _ = self._get_file_info(path)

        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO page_text
                   (file_path, page_num, file_mtime,
                    text, text_length)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (path, page_num, mtime, text, len(text))
                    for page_num, text in pages.items()
                ],
            )

            if self.fts_available:
                page_nums = list(pages.keys())
                placeholders = ",".join("?" * len(page_nums))
                conn.execute(
                    f"DELETE FROM pdf_search_fts"
                    f" WHERE file_path = ? AND page_num IN ({placeholders})",
                    (path, *page_nums),
                )
                conn.executemany(
                    "INSERT INTO pdf_search_fts (file_path, page_num, text)"
                    " VALUES (?, ?, ?)",
                    [(path, pn, txt) for pn, txt in pages.items()],
                )

    # ==================== Image Operations ====================

    def get_page_images(self, path: str, page_num: int) -> list[dict[str, Any]] | None:
        """
        Get cached images for a specific page.

        Args:
            path: Path to PDF file
            page_num: Page number (0-indexed)

        Returns:
            List of image dicts or None if not cached/invalid
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT image_index, width, height,
                   format, file_path_on_disk, size_bytes, file_mtime
                   FROM page_images
                   WHERE file_path = ? AND page_num = ?
                   ORDER BY image_index""",
                (path, page_num),
            ).fetchall()

            if not rows:
                return None

            # Check if any row is invalid
            if not all(self._is_cache_valid(path, row["file_mtime"]) for row in rows):
                return None

            real_rows = [row for row in rows if row["image_index"] >= 0]

            for row in real_rows:
                if not Path(row["file_path_on_disk"]).exists():
                    return None  # triggers re-extraction

            return [
                {
                    "page": page_num + 1,
                    "index": row["image_index"],
                    "width": row["width"],
                    "height": row["height"],
                    "format": row["format"],
                    "path": row["file_path_on_disk"],
                    "size_bytes": row["size_bytes"],
                }
                for row in real_rows
            ]

    def save_page_images(
        self, path: str, page_num: int, images: list[dict[str, Any]]
    ) -> None:
        """
        Save page images to cache.

        Args:
            path: Path to PDF file
            page_num: Page number (0-indexed)
            images: List of image dicts with width, height, format, path, size_bytes
        """
        mtime, _ = self._get_file_info(path)

        with sqlite3.connect(self.db_path) as conn:
            if not images:
                old_rows = conn.execute(
                    "SELECT file_path_on_disk FROM page_images"
                    " WHERE file_path = ? AND page_num = ?",
                    (path, page_num),
                ).fetchall()
                for row in old_rows:
                    if row[0] != "__sentinel__":
                        try:
                            Path(row[0]).unlink()
                        except FileNotFoundError:
                            pass
                conn.execute(
                    "DELETE FROM page_images" " WHERE file_path = ? AND page_num = ?",
                    (path, page_num),
                )
                conn.execute(
                    "INSERT INTO page_images (file_path, page_num,"
                    " image_index, file_mtime, width, height, format,"
                    " file_path_on_disk, size_bytes)"
                    " VALUES (?, ?, -1, ?, 0, 0, 'sentinel',"
                    " '__sentinel__', 0)",
                    (path, page_num, mtime),
                )
                return

            # Query existing file paths for orphan cleanup
            old_rows = conn.execute(
                "SELECT file_path_on_disk FROM page_images"
                " WHERE file_path = ? AND page_num = ?",
                (path, page_num),
            ).fetchall()
            old_paths = {row[0] for row in old_rows}
            new_paths = {img["path"] for img in images}
            orphans = old_paths - new_paths

            # Delete orphan files from disk
            for orphan_path in orphans:
                if orphan_path != "__sentinel__":
                    try:
                        Path(orphan_path).unlink()
                    except FileNotFoundError:
                        pass

            # Clear existing DB rows for this page
            conn.execute(
                "DELETE FROM page_images WHERE file_path = ? AND page_num = ?",
                (path, page_num),
            )

            # Insert new images
            conn.executemany(
                """INSERT INTO page_images
                   (file_path, page_num, image_index,
                    file_mtime, width, height, format,
                    file_path_on_disk, size_bytes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        path,
                        page_num,
                        img.get("index", i),
                        mtime,
                        img["width"],
                        img["height"],
                        img["format"],
                        img["path"],
                        img["size_bytes"],
                    )
                    for i, img in enumerate(images)
                ],
            )

    # ==================== Table Operations ====================

    def get_page_tables(self, path: str, page_num: int) -> list[dict[str, Any]] | None:
        """Get cached tables for a specific page. Returns None if not cached/invalid."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT data, file_mtime FROM page_tables"
                " WHERE file_path = ? AND page_num = ?",
                (path, page_num),
            ).fetchone()
            if row is None:
                return None
            if not self._is_cache_valid(path, row[1]):
                return None
            return cast(list[dict[str, Any]], json.loads(row[0]))

    def save_page_tables(
        self, path: str, page_num: int, tables: list[dict[str, Any]]
    ) -> None:
        """Save page tables to cache. Stores empty list [] as sentinel for tableless pages."""  # noqa: E501
        mtime, _ = self._get_file_info(path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO page_tables"
                " (file_path, page_num, file_mtime, data) VALUES (?, ?, ?, ?)",
                (path, page_num, mtime, json.dumps(tables)),
            )

    # ==================== Embedding Operations ====================

    def get_page_embeddings(
        self, path: str, page_nums: list[int], model_name: str
    ) -> dict[int, bytes]:
        """
        Get cached raw embedding bytes for multiple pages.

        Deletes any rows stored under a different model before querying,
        so the caller always gets embeddings consistent with model_name.

        Returns a dict mapping 0-indexed page_num to the raw float32 bytes
        for each page whose mtime is still valid. Pages not in cache or with
        a stale mtime are omitted.

        The caller is responsible for converting bytes to a numpy array:
            np.frombuffer(blob, dtype=np.float32).copy()

        Returns {} when page_nums is empty.
        """
        if not page_nums:
            return {}

        placeholders = ",".join("?" * len(page_nums))
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM page_embeddings WHERE file_path = ? AND model != ?",
                (path, model_name),
            )
            rows = conn.execute(
                f"SELECT page_num, embedding, file_mtime"
                f" FROM page_embeddings"
                f" WHERE file_path = ? AND page_num IN ({placeholders})"
                f" AND model = ?",
                (path, *page_nums, model_name),
            ).fetchall()

        result: dict[int, bytes] = {}
        for page_num, blob, mtime in rows:
            if self._is_cache_valid(path, mtime):
                result[int(page_num)] = bytes(blob)
        return result

    def save_page_embeddings(
        self, path: str, embeddings: dict[int, bytes], model_name: str
    ) -> None:
        """
        Save raw embedding bytes to cache.

        Args:
            path: Path to PDF file.
            embeddings: Dict mapping 0-indexed page_num to raw float32 bytes.
                        Use ndarray.tobytes() to convert from numpy.
            model_name: fastembed model identifier (stored alongside the blob).
        """
        if not embeddings:
            return

        mtime, _ = self._get_file_info(path)
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO page_embeddings"
                " (file_path, page_num, file_mtime, embedding, model)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (path, page_num, mtime, blob, model_name)
                    for page_num, blob in embeddings.items()
                ],
            )

    def get_section_embeddings(
        self, path: str, section_ids: list[int]
    ) -> dict[int, bytes]:
        """Get cached raw embedding bytes for multiple sections of a PDF.

        Returns {section_id: blob} for sections whose mtime is still
        valid. Sections not in cache or with stale mtime are omitted.
        """
        if not section_ids:
            return {}

        placeholders = ",".join("?" * len(section_ids))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT section_id, embedding, file_mtime"
                f" FROM section_embeddings"
                f" WHERE file_path = ? AND section_id IN ({placeholders})",
                (path, *section_ids),
            ).fetchall()

        result: dict[int, bytes] = {}
        for section_id, blob, mtime in rows:
            if self._is_cache_valid(path, mtime):
                result[int(section_id)] = bytes(blob)
        return result

    def save_section_embeddings(
        self,
        path: str,
        embeddings: dict[int, bytes],
        section_keys: dict[int, str],
        model: str,
    ) -> None:
        """Save section embedding blobs (idempotent INSERT OR REPLACE).

        Args:
            path: Path to PDF file.
            embeddings: {section_id: float32 blob}.
            section_keys: {section_id: stable string key}.
            model: Embedding model identifier.
        """
        if not embeddings:
            return

        mtime, _ = self._get_file_info(path)
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO section_embeddings"
                " (file_path, section_id, section_key, file_mtime,"
                "  embedding, model)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (path, sid, section_keys[sid], mtime, blob, model)
                    for sid, blob in embeddings.items()
                ],
            )

    # ==================== Render Operations ====================

    def get_page_render(
        self, path: str, page_num: int, dpi: int
    ) -> dict[str, Any] | None:
        """Get cached render for a page at a specific DPI.

        Returns None if not cached."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT file_path_on_disk, size_bytes, width, height, file_mtime
                   FROM page_renders
                   WHERE file_path = ? AND page_num = ? AND dpi = ?""",
                (path, page_num, dpi),
            ).fetchone()
            if row is None:
                return None
            if not self._is_cache_valid(path, row["file_mtime"]):
                return None
            if not Path(row["file_path_on_disk"]).exists():
                return None
            return {
                "file_path_on_disk": row["file_path_on_disk"],
                "size_bytes": row["size_bytes"],
                "width": row["width"],
                "height": row["height"],
            }

    def save_page_render(
        self,
        path: str,
        page_num: int,
        file_mtime: float,
        dpi: int,
        render_dict: dict[str, Any],
    ) -> None:
        """Save a render to cache.

        Unlinks the old PNG if the path changed (orphan guard)."""
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT file_path_on_disk FROM page_renders"
                " WHERE file_path = ? AND page_num = ? AND dpi = ?",
                (path, page_num, dpi),
            ).fetchone()
            if existing and existing[0] != render_dict["file_path_on_disk"]:
                try:
                    Path(existing[0]).unlink()
                except FileNotFoundError:
                    pass
            conn.execute(
                """INSERT OR REPLACE INTO page_renders
                   (file_path, page_num, file_mtime, dpi,
                    file_path_on_disk, size_bytes, width, height)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    path,
                    page_num,
                    file_mtime,
                    dpi,
                    render_dict["file_path_on_disk"],
                    render_dict["size_bytes"],
                    render_dict["width"],
                    render_dict["height"],
                ),
            )

    # ==================== Cache Management ====================

    def _invalidate_file(self, path: str) -> None:
        """Remove all cache entries for a file."""
        with sqlite3.connect(self.db_path) as conn:
            # Delete image files from disk before removing DB rows
            rows = conn.execute(
                "SELECT file_path_on_disk FROM page_images WHERE file_path = ?",
                (path,),
            ).fetchall()
            for row in rows:
                if row[0] != "__sentinel__":
                    try:
                        Path(row[0]).unlink()
                    except FileNotFoundError:
                        pass

            # Delete render PNG files before removing DB rows
            render_rows = conn.execute(
                "SELECT file_path_on_disk FROM page_renders WHERE file_path = ?",
                (path,),
            ).fetchall()
            for (render_path,) in render_rows:
                try:
                    Path(render_path).unlink()
                except FileNotFoundError:
                    pass
            conn.execute("DELETE FROM page_renders WHERE file_path = ?", (path,))

            conn.execute("DELETE FROM pdf_metadata WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM page_text WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM page_images WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM page_tables WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM page_embeddings WHERE file_path = ?", (path,))
            conn.execute(
                "DELETE FROM section_embeddings WHERE file_path = ?",
                (path,),
            )
            if self.fts_available:
                conn.execute("DELETE FROM pdf_search_fts WHERE file_path = ?", (path,))

    def clear_expired(self) -> int:
        """
        Remove expired cache entries.

        Returns:
            Number of files cleared
        """
        cutoff = (datetime.now() - timedelta(hours=self.ttl_hours)).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            # Get expired file paths
            expired = conn.execute(
                "SELECT file_path FROM pdf_metadata WHERE accessed_at < ?", (cutoff,)
            ).fetchall()

            expired_paths = [row[0] for row in expired]

            if expired_paths:
                placeholders = ",".join("?" * len(expired_paths))

                # Delete image files from disk
                img_rows = conn.execute(
                    f"SELECT file_path_on_disk FROM page_images"
                    f" WHERE file_path IN ({placeholders})",
                    expired_paths,
                ).fetchall()
                for row in img_rows:
                    if row[0] != "__sentinel__":
                        try:
                            Path(row[0]).unlink()
                        except FileNotFoundError:
                            pass

                conn.execute(
                    f"DELETE FROM pdf_metadata WHERE file_path IN ({placeholders})",
                    expired_paths,
                )
                conn.execute(
                    f"DELETE FROM page_text WHERE file_path IN ({placeholders})",
                    expired_paths,
                )
                conn.execute(
                    f"DELETE FROM page_images WHERE file_path IN ({placeholders})",
                    expired_paths,
                )
                conn.execute(
                    f"DELETE FROM page_tables WHERE file_path IN ({placeholders})",
                    expired_paths,
                )
                conn.execute(
                    f"DELETE FROM page_embeddings"
                    f" WHERE file_path IN ({placeholders})",
                    expired_paths,
                )
                conn.execute(
                    f"DELETE FROM section_embeddings"
                    f" WHERE file_path IN ({placeholders})",
                    expired_paths,
                )
                if self.fts_available:
                    conn.execute(
                        f"DELETE FROM pdf_search_fts"
                        f" WHERE file_path IN ({placeholders})",
                        expired_paths,
                    )

                # Delete render PNG files for expired paths
                render_rows = conn.execute(
                    f"SELECT file_path_on_disk FROM page_renders"
                    f" WHERE file_path IN ({placeholders})",
                    expired_paths,
                ).fetchall()
                for (render_path,) in render_rows:
                    try:
                        Path(render_path).unlink()
                    except FileNotFoundError:
                        pass
                conn.execute(
                    f"DELETE FROM page_renders WHERE file_path IN ({placeholders})",
                    expired_paths,
                )

        # Sweep page_renders for stale-mtime entries (PDF file changed)
        with sqlite3.connect(self.db_path) as conn2:
            stale_paths = conn2.execute(
                "SELECT DISTINCT file_path FROM page_renders"
            ).fetchall()
            for (rpath,) in stale_paths:
                sample_row = conn2.execute(
                    "SELECT file_mtime FROM page_renders WHERE file_path = ? LIMIT 1",
                    (rpath,),
                ).fetchone()
                if sample_row and not self._is_cache_valid(rpath, sample_row[0]):
                    stale_render_rows = conn2.execute(
                        "SELECT file_path_on_disk FROM page_renders"
                        " WHERE file_path = ?",
                        (rpath,),
                    ).fetchall()
                    for (fp,) in stale_render_rows:
                        try:
                            Path(fp).unlink()
                        except FileNotFoundError:
                            pass
                    conn2.execute(
                        "DELETE FROM page_renders WHERE file_path = ?", (rpath,)
                    )

        return len(expired_paths)

    def clear_all(self) -> int:
        """Clear entire cache. Returns number of files cleared."""
        # Delete all image files and render files
        shutil.rmtree(self.images_dir, ignore_errors=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.images_dir), 0o700)
        shutil.rmtree(self.renders_dir, ignore_errors=True)
        self.renders_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.renders_dir), 0o700)

        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM pdf_metadata").fetchone()[0]
            conn.execute("DELETE FROM pdf_metadata")
            conn.execute("DELETE FROM page_text")
            conn.execute("DELETE FROM page_images")
            conn.execute("DELETE FROM page_tables")
            conn.execute("DELETE FROM page_embeddings")
            conn.execute("DELETE FROM section_embeddings")
            conn.execute("DELETE FROM page_renders")
            if self.fts_available:
                conn.execute("DELETE FROM pdf_search_fts")
            return int(count)

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with sqlite3.connect(self.db_path) as conn:
            stats = {}

            # Count files
            stats["total_files"] = conn.execute(
                "SELECT COUNT(*) FROM pdf_metadata"
            ).fetchone()[0]

            # Count pages
            stats["total_pages"] = conn.execute(
                "SELECT COUNT(*) FROM page_text"
            ).fetchone()[0]

            # Count images (exclude sentinel rows)
            stats["total_images"] = conn.execute(
                "SELECT COUNT(*) FROM page_images WHERE image_index >= 0"
            ).fetchone()[0]

            stats["total_tables"] = conn.execute(
                "SELECT COALESCE(SUM(json_array_length(data)), 0) FROM page_tables"
            ).fetchone()[0]

            stats["embedding_pages"] = conn.execute(
                "SELECT COUNT(*) FROM page_embeddings"
            ).fetchone()[0]

            stats["total_renders"] = conn.execute(
                "SELECT COUNT(*) FROM page_renders"
            ).fetchone()[0]

            # FTS5 indexed page count
            if self.fts_available:
                stats["fts_indexed_pages"] = conn.execute(
                    "SELECT COUNT(*) FROM pdf_search_fts"
                ).fetchone()[0]
            else:
                stats["fts_indexed_pages"] = 0

            # Total text size
            row = conn.execute("SELECT SUM(text_length) FROM page_text").fetchone()
            stats["total_text_chars"] = row[0] or 0

            # Database file size + image directory size + renders directory size
            try:
                images_size = sum(
                    f.stat().st_size for f in self.images_dir.glob("*.png")
                )
            except FileNotFoundError:
                images_size = 0
            try:
                renders_size = sum(
                    f.stat().st_size for f in self.renders_dir.glob("*.png")
                )
            except FileNotFoundError:
                renders_size = 0
            stats["cache_size_bytes"] = (
                os.path.getsize(self.db_path) + images_size + renders_size
            )
            stats["cache_size_mb"] = round(stats["cache_size_bytes"] / (1024 * 1024), 2)

            return stats

    # ==================== FTS5 Search Operations ====================

    def search_fts(
        self,
        path: str,
        query: str,
        max_results: int,
        context_chars: int,
    ) -> list[dict[str, Any]]:
        """
        Search the FTS5 index for pages matching query.

        Returns at most max_results results sorted by descending BM25 relevance.
        Each result has keys: page (1-indexed), excerpt (str), score (float >= 0).
        Returns [] when fts_available is False or no matches found.

        Args:
            path: Path to PDF file (must match the value stored at index time)
            query: Search query (Porter stemming applied; FTS5 operators escaped)
            max_results: Maximum number of results to return
            context_chars: Approximate characters of context in excerpts
        """
        if not self.fts_available:
            return []

        escaped = _escape_fts5_query(query)
        # Map context_chars to FTS5 snippet token count (approximate)
        num_tokens = max(4, min(64, context_chars // 5))

        with sqlite3.connect(self.db_path) as conn:
            try:
                rows = conn.execute(
                    "SELECT page_num,"
                    " snippet(pdf_search_fts, 2, '', '', '...', ?),"
                    " -bm25(pdf_search_fts)"
                    " FROM pdf_search_fts"
                    " WHERE pdf_search_fts MATCH ? AND file_path = ?"
                    " ORDER BY bm25(pdf_search_fts)"
                    " LIMIT ?",
                    (num_tokens, escaped, path, max_results),
                ).fetchall()
            except sqlite3.OperationalError:
                return []

        return [
            {
                "page": int(page_num) + 1,
                "excerpt": excerpt or "",
                "score": float(score),
            }
            for page_num, excerpt, score in rows
        ]

    def get_fts_page_counts(self, path: str, query: str) -> dict[int, int]:
        """
        Return per-page token-occurrence counts for query.

        Queries the FTS5 index for ALL matching pages (no LIMIT) using the
        same tokenised AND semantics as `_escape_fts5_query`. For each
        matched page, sums case-insensitive occurrences of every query
        token in the stored text — a per-page intensity signal that
        agrees with the retrieval path (so pages returned in `matches`
        are guaranteed to appear here).

        Returns a dict mapping 0-indexed page_num to total token-occurrence
        count. Returns {} when fts_available is False, the query has no
        usable tokens, or no pages match.
        """
        if not self.fts_available:
            return {}

        tokens_lower = [_FTS_TOKEN_STRIP.sub("", tok).lower() for tok in query.split()]
        tokens_lower = [t for t in tokens_lower if t]
        if not tokens_lower:
            return {}

        escaped = _escape_fts5_query(query)

        with sqlite3.connect(self.db_path) as conn:
            try:
                rows = conn.execute(
                    "SELECT page_num, text"
                    " FROM pdf_search_fts"
                    " WHERE pdf_search_fts MATCH ? AND file_path = ?",
                    (escaped, path),
                ).fetchall()
            except sqlite3.OperationalError:
                return {}

        result: dict[int, int] = {}
        for page_num, text in rows:
            text_lower = text.lower()
            count = sum(text_lower.count(t) for t in tokens_lower)
            if count > 0:
                result[int(page_num)] = count
        return result

    def get_fts_index_coverage(self, path: str) -> tuple[int, int]:
        """
        Return (fts_indexed_pages, total_cached_pages) for path.

        When fts_available is False, returns (0, page_text_count) so that
        the FTS eligibility check (indexed == total > 0) never fires
        on a file that has cached page_text rows but no FTS rows.
        """
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM page_text WHERE file_path = ?",
                (path,),
            ).fetchone()[0]

            if not self.fts_available:
                return (0, int(total))

            indexed = conn.execute(
                "SELECT COUNT(*) FROM pdf_search_fts WHERE file_path = ?",
                (path,),
            ).fetchone()[0]

        return (int(indexed), int(total))

    def index_sections(self, path: str, sections: list[Section]) -> None:
        """
        Replace the cached section FTS5 entries for `path` with the given list.

        Uses DELETE + INSERT for atomic replacement (FTS5 lacks PRIMARY KEY,
        matching the existing pattern for page indexing).

        No-op if FTS5 is unavailable on this SQLite build.
        """
        if not self.fts_available:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM pdf_section_fts WHERE file_path = ?", (path,))
            if sections:
                conn.executemany(
                    "INSERT INTO pdf_section_fts"
                    " (file_path, section_id, title, text,"
                    " start_page, end_page, title_source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            path,
                            i,
                            s.title,
                            s.text,
                            s.start_page,
                            s.end_page,
                            s.title_source,
                        )
                        for i, s in enumerate(sections)
                    ],
                )
            conn.execute("DELETE FROM pdf_section_fts_cjk WHERE file_path = ?", (path,))
            cjk_sections = [
                (
                    path,
                    i,
                    _cjk_split(s.title or ""),
                    _cjk_split(s.text or ""),
                    s.start_page,
                    s.end_page,
                    s.title_source,
                )
                for i, s in enumerate(sections)
                if _contains_cjk(s.title or "") or _contains_cjk(s.text or "")
            ]
            if cjk_sections:
                conn.executemany(
                    "INSERT INTO pdf_section_fts_cjk"
                    " (file_path, section_id, title, text,"
                    " start_page, end_page, title_source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    cjk_sections,
                )

    def search_section_fts(
        self,
        path: str,
        query: str,
        max_results: int,
    ) -> list[dict[str, Any]]:
        """
        Search the section FTS5 index for sections matching the query.

        Returns at most max_results results sorted by descending BM25 relevance.
        Each result has keys: section_id (int), title (str), start_page (int),
        end_page (int), score (float >= 0).

        Returns [] when fts_available is False or no matches found.

        Args:
            path: Path to PDF file (must match the value stored at index time)
            query: Search query (Porter stemming applied; FTS5 operators escaped)
            max_results: Maximum number of results to return
        """
        if not self.fts_available:
            return []
        escaped = _escape_fts5_query(query)
        with sqlite3.connect(self.db_path) as conn:
            try:
                rows = conn.execute(
                    "SELECT section_id, title, start_page, end_page,"
                    " title_source, -bm25(pdf_section_fts)"
                    " FROM pdf_section_fts"
                    " WHERE pdf_section_fts MATCH ? AND file_path = ?"
                    " ORDER BY bm25(pdf_section_fts)"
                    " LIMIT ?",
                    (escaped, path, max_results),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [
            {
                "section_id": int(sid),
                "title": title,
                "start_page": int(sp),
                "end_page": int(ep),
                "title_source": title_source,
                "score": float(score),
            }
            for sid, title, sp, ep, title_source, score in rows
        ]

    def get_section_fts_coverage(self, path: str) -> int:
        """
        Return the number of indexed sections for `path`. 0 means no index
        populated yet (or FTS5 unavailable).
        """
        if not self.fts_available:
            return 0
        with sqlite3.connect(self.db_path) as conn:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM pdf_section_fts WHERE file_path = ?",
                    (path,),
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
        return int(row[0]) if row else 0

    def get_section_embeddings_coverage(self, path: str) -> int:
        """Return the number of cached, valid section embeddings for `path`."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT file_mtime FROM section_embeddings WHERE file_path = ?",
                (path,),
            ).fetchall()
        return sum(1 for (mtime,) in rows if self._is_cache_valid(path, mtime))
