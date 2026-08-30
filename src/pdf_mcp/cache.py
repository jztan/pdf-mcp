"""
SQLite-based cache for PDF data persistence across MCP server restarts.
"""

import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, cast

from pdf_mcp import content_trust
from pdf_mcp.chart_extractor import CHART_EXTRACTION_VERSION
from pdf_mcp.extractor import TABLE_EXTRACTION_VERSION
from pdf_mcp.embedder import DEFAULT_MODEL
from pdf_mcp.section_detector import Section

logger = logging.getLogger(__name__)

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
# 13: ff/ffi ligature halves kept
# 12: spanning bands must be wide, not just two-sided (table cells)
# 11: spanning rows no longer split at the gutter
# 10: layout-checked chunked embeddings (9 was interim)
_EXTRACTION_VERSION = 13

# Per-connection pragmas. Both reset on every open, unlike journal_mode which
# is persistent in the database file (see _connect / _init_db).
_BUSY_TIMEOUT_MS = 5000

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


def _fts5_or_fallback(query: str) -> str | None:
    """
    OR-joined variant of `_escape_fts5_query`, or None when the query does
    not qualify for a retry.

    Callers run this only after the AND form matched no rows. A question-
    shaped query ("Greater China net sales decline in 2024") otherwise
    returns nothing whenever a single word is absent from the page --
    "decline" where the filing says "decreased" -- even though the rest of
    the query identifies the page precisely. BM25 still ranks pages
    carrying more (and rarer) query terms first, so recovered results stay
    ordered sensibly.

    Queries of one or two tokens do NOT qualify. Two terms is a deliberate
    conjunction ("pgvector unicorn"), where AND's guarantee that every term
    is present is the point; relaxing it there would trade precision for
    recall in exactly the case the caller was being specific. The fallback
    targets longer queries, where some words are incidental connective
    tissue rather than search terms.
    """
    tokens: list[str] = []
    for raw in query.split():
        cleaned = _FTS_TOKEN_STRIP.sub("", raw)
        if cleaned:
            tokens.append(f'"{cleaned}"')
    if len(tokens) < 3:
        return None
    return " OR ".join(tokens)


def _escape_fts5_query_cjk(query: str) -> str:
    """Escape a query for the char-split CJK FTS index.

    Delegates each whitespace token to _cjk_split (the same function the write
    path uses, guaranteeing identical tokenization), strips FTS5 reserved
    characters, and wraps the split token in one double-quoted phrase so the
    chars must be positionally adjacent. Tokens are AND-joined.
    """
    phrases: list[str] = []
    for raw in query.split():
        split = _cjk_split(raw)
        cleaned = _FTS_TOKEN_STRIP.sub("", split).strip()
        if cleaned:
            phrases.append(f'"{cleaned}"')
    if not phrases:
        return _NO_MATCH_SENTINEL
    return " ".join(phrases)


def _get_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Return column names for a table, or empty set if the table does not exist."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


# One row per page, chosen deterministically. With ocr_lang in the primary key
# a page can hold several rows, so every read must say which one it means.
#
# Language-aware callers (the OCR path) pass a language and match either that
# row or the '' sentinel, since extracted text is language-independent and
# suppresses OCR whatever was asked for. Precedence within that:
#   1. usable text first: extracting a scanned page yields '', and that empty
#      row must not shadow real OCR text or the page would be re-OCR'd.
#      Tested with `text = ''` rather than LENGTH(text) = 0, because LENGTH
#      stops at an embedded NUL and real cached pages do contain them
#   2. then the extracted row, because a page with a real text layer is never
#      OCR'd whatever language is requested
#   3. then most recent
#
# Language-unaware callers get the most recently written row. created_at has
# one-second resolution, so rowid is what actually breaks ties.
#
# ROW_NUMBER() needs SQLite 3.25+ (2018). Verified 3.51 on the dev machine
# and 3.46 in the python:3.13-slim runtime image the Dockerfile builds on.
_PICK_BY_LANG = """
    SELECT {columns} FROM (
        SELECT {columns}, ROW_NUMBER() OVER (
            PARTITION BY page_num
            ORDER BY (text = '') ASC,
                     (ocr_lang = '') DESC,
                     rowid DESC
        ) AS rn
        FROM page_text
        WHERE file_path = ? AND page_num IN ({placeholders})
          AND ocr_lang IN (?, '')
    ) WHERE rn = 1
"""

_PICK_LATEST = """
    SELECT {columns} FROM (
        SELECT {columns}, ROW_NUMBER() OVER (
            PARTITION BY page_num
            ORDER BY created_at DESC, rowid DESC
        ) AS rn
        FROM page_text
        WHERE file_path = ? AND page_num IN ({placeholders})
    ) WHERE rn = 1
"""


def _page_rows_query(columns: str, n_pages: int, by_lang: bool) -> str:
    """Build a one-row-per-page SELECT over page_text."""
    template = _PICK_BY_LANG if by_lang else _PICK_LATEST
    return template.format(columns=columns, placeholders=",".join("?" * n_pages))


def _page_text_pk(conn: sqlite3.Connection) -> list[str]:
    """Primary-key column names for page_text, in key order."""
    rows = conn.execute("PRAGMA table_info(page_text)").fetchall()
    keyed = [(row[5], row[1]) for row in rows if row[5]]
    return [name for _, name in sorted(keyed)]


def _migrate_page_text_pk(conn: sqlite3.Connection) -> bool:
    """Widen page_text's PK to (file_path, page_num, ocr_lang).

    SQLite cannot ALTER a primary key, so this is create-copy-drop-rename.
    Detected by PK shape rather than a version counter, because
    _EXTRACTION_VERSION DROPS page_text and this migration must preserve every
    cached page (issue #27).

    Returns True when a migration ran. Idempotent: a no-op once the key has
    three columns, and on fresh databases created with the current schema.
    """
    if _page_text_pk(conn) != ["file_path", "page_num"]:
        return False

    # Old tables predate several columns (created_at is never added by an
    # ALTER, and has_hidden_text may not exist yet), so the copy cannot name
    # columns it has not checked for. Missing ones fall back to the default
    # the current schema would have given them.
    old_cols = _get_columns(conn, "page_text")
    fallbacks = {
        "source": "'extracted'",
        "created_at": "CURRENT_TIMESTAMP",
        "has_hidden_text": "NULL",
        "ocr_lang": "''",
    }
    select_terms = []
    for column in (
        "file_path",
        "page_num",
        "file_mtime",
        "text",
        "text_length",
        "source",
        "created_at",
        "has_hidden_text",
        "ocr_lang",
    ):
        if column == "ocr_lang":
            select_terms.append(
                "LOWER(TRIM(COALESCE(ocr_lang, '')))" if column in old_cols else "''"
            )
        elif column in old_cols:
            select_terms.append(column)
        else:
            select_terms.append(fallbacks[column])

    conn.executescript(
        """
        CREATE TABLE page_text_new (
            file_path TEXT NOT NULL,
            page_num INTEGER NOT NULL,
            file_mtime REAL NOT NULL,
            text TEXT NOT NULL,
            text_length INTEGER NOT NULL,
            source TEXT DEFAULT 'extracted',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            has_hidden_text INTEGER DEFAULT NULL,
            ocr_lang TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (file_path, page_num, ocr_lang)
        );
        INSERT INTO page_text_new
            (file_path, page_num, file_mtime, text, text_length,
             source, created_at, has_hidden_text, ocr_lang)
        SELECT """
        + ", ".join(select_terms)
        + """
        FROM page_text;
        DROP TABLE page_text;
        ALTER TABLE page_text_new RENAME TO page_text;
        CREATE INDEX IF NOT EXISTS idx_page_text_path
            ON page_text(file_path);
        """
    )
    return True


def normalize_ocr_lang(lang: str | None) -> str:
    """Canonical cache-key form of an ocr_lang argument.

    Case and surrounding whitespace never change what Tesseract does, so they
    must not create separate cache rows. Language ORDER does change Tesseract's
    output, so it is preserved exactly (issue #27).

    The '' return is the sentinel for "not OCR text" and is what non-OCR rows
    store in page_text.ocr_lang.
    """
    return (lang or "").strip().lower()


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

    def _connect(self) -> sqlite3.Connection:
        """Open a cache connection with the per-connection pragmas applied.

        `journal_mode` is persistent in the database file and is set once in
        `_init_db`. `synchronous` and `busy_timeout` are per-connection and
        revert on every open, so they belong here: the cache opens ~42
        connections and a pragma set only at init would lapse on all but one.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        # NORMAL is only safe under WAL, where it costs at most the most
        # recent transactions on an OS crash and the database stays intact.
        # Under a rollback journal it weakens the write ordering that keeps
        # the journal recoverable, so leave SQLite's FULL default alone.
        if getattr(self, "journal_mode", None) == "wal":
            conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        """
        Initialize database schema.

        Side effect: sets self.fts_available (bool) indicating whether
        the SQLite build supports FTS5 virtual tables.
        """
        migrated_pk = False
        with self._connect() as conn:
            # WAL is persistent in the database file, so it is set once here
            # rather than on every connect. It must run before any statement
            # opens an implicit transaction: journal_mode is a no-op inside
            # one. PRAGMA returns the resulting mode, so a filesystem that
            # refuses WAL (network mounts) is detected by value, not by
            # exception, and simply stays in rollback mode.
            self.journal_mode = str(
                conn.execute("PRAGMA journal_mode=wal").fetchone()[0]
            ).lower()
            if self.journal_mode != "wal":
                logger.warning(
                    "SQLite refused WAL mode at %s (got %r); cache stays in "
                    "rollback mode. Cache writes will be slower on Windows.",
                    self.db_path,
                    self.journal_mode,
                )

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
                # Char-split CJK mirrors are rebuilt from page_text on open;
                # stale rows must go with it or the backfill skips them.
                conn.execute("DROP TABLE IF EXISTS pdf_search_fts_cjk")
                conn.execute("DROP TABLE IF EXISTS pdf_section_fts_cjk")
                # Derived from the same extraction pipeline as page_text.
                conn.execute("DROP TABLE IF EXISTS page_blocks")
                # Head text and term counts derive from page_text.
                conn.execute("DROP TABLE IF EXISTS doc_profiles")
            if extraction_version < _EXTRACTION_VERSION:
                conn.execute(f"PRAGMA user_version = {_EXTRACTION_VERSION}")

            # page_images: drop if old binary schema OR missing geometry_json
            # (adds bbox/placements). Column-presence drop re-extracts images
            # only — deliberately NOT an _EXTRACTION_VERSION bump, which would
            # also wipe page_text/page_embeddings/FTS and force a re-embed.
            cols = _get_columns(conn, "page_images")
            if (
                "data" in cols
                or (cols and "file_path_on_disk" not in cols)
                or (cols and "geometry_json" not in cols)
            ):
                conn.execute("DROP TABLE IF EXISTS page_images")

            # page_tables: introduced in v1.5.0 — older caches may lack 'data'.
            # A cache without 'extraction_version' was written before tables
            # were extracted out-of-process, so every numeric cell in it may
            # have a detached decimal point ("4.5" stored as "45\n."). Those
            # rows are not merely stale, they are wrong, so drop rather than
            # migrate. Deliberately NOT an _EXTRACTION_VERSION bump, which
            # would also wipe page_text/page_embeddings/FTS and force a
            # re-embed of every warmed corpus.
            cols = _get_columns(conn, "page_tables")
            if cols and ("data" not in cols or "extraction_version" not in cols):
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

            # page_renders: drop if missing required columns. codec/quality
            # joined the PRIMARY KEY (a JPEG must not be served to a caller who
            # asked for the same page and DPI as PNG), and SQLite cannot ALTER
            # a primary key, so an old table is dropped. Unlink its images
            # first: dropping the rows alone would strand the files in the
            # renders dir with nothing left pointing at them.
            cols = _get_columns(conn, "page_renders")
            if cols and not {
                "file_path",
                "page_num",
                "dpi",
                "codec",
                "quality",
                "file_path_on_disk",
            }.issubset(cols):
                for (stale,) in conn.execute(
                    "SELECT file_path_on_disk FROM page_renders"
                ).fetchall():
                    try:
                        Path(stale).unlink()
                    except OSError:
                        pass
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
                    accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    content_trust_json TEXT DEFAULT NULL
                );

                -- Global content-trust version stamp (single row, id=0).
                -- Replaces per-document trust_version in pdf_metadata.
                CREATE TABLE IF NOT EXISTS content_trust_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 0),
                    trust_version INTEGER NOT NULL
                );

                -- Page text cache. ocr_lang is part of the primary key: the
                -- same page OCR'd under two language strings is two different
                -- results (Tesseract's output depends on language order), so
                -- they coexist rather than evict each other (issue #27).
                -- '' is the sentinel for non-OCR text. NOT NULL because SQLite
                -- permits NULL in a non-INTEGER primary key column, which
                -- would stop extracted rows being unique.
                CREATE TABLE IF NOT EXISTS page_text (
                    file_path TEXT NOT NULL,
                    page_num INTEGER NOT NULL,
                    file_mtime REAL NOT NULL,
                    text TEXT NOT NULL,
                    text_length INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    has_hidden_text INTEGER DEFAULT NULL,
                    ocr_lang TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (file_path, page_num, ocr_lang)
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
                    geometry_json TEXT DEFAULT NULL,
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
                    extraction_version INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (file_path, page_num)
                );

                CREATE INDEX IF NOT EXISTS idx_page_tables_path
                    ON page_tables(file_path);

                -- Page embeddings cache (raw float32 BLOBs for semantic search)
                CREATE TABLE IF NOT EXISTS page_embeddings (
                    file_path   TEXT    NOT NULL,
                    page_num    INTEGER NOT NULL,
                    chunk_idx   INTEGER NOT NULL DEFAULT 0,
                    file_mtime  REAL    NOT NULL,
                    embedding   BLOB    NOT NULL,
                    model       TEXT    NOT NULL DEFAULT 'BAAI/bge-small-en-v1.5',
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_path, page_num, chunk_idx)
                );

                CREATE INDEX IF NOT EXISTS idx_page_embeddings_path
                    ON page_embeddings(file_path);

                -- Per-document profile: head vector (page 1, first
                -- PROFILE_HEAD_CHARS chars) for the corpus search's document
                -- arm, plus term counts for overview `about`. One row per
                -- document; mtime + model validated on read like
                -- page_embeddings. embedding is NULL when page 1 had no text.
                CREATE TABLE IF NOT EXISTS doc_profiles (
                    file_path   TEXT    NOT NULL,
                    file_mtime  REAL    NOT NULL,
                    model       TEXT    NOT NULL,
                    head_chars  INTEGER NOT NULL,
                    embedding   BLOB,
                    terms       TEXT    NOT NULL,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_path)
                );

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
                    codec              TEXT    NOT NULL DEFAULT 'png',
                    quality            INTEGER NOT NULL DEFAULT 0,
                    file_path_on_disk  TEXT    NOT NULL,
                    size_bytes         INTEGER NOT NULL,
                    width              INTEGER NOT NULL,
                    height             INTEGER NOT NULL,
                    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_path, page_num, dpi, codec, quality)
                );

                CREATE INDEX IF NOT EXISTS idx_page_renders_path
                    ON page_renders(file_path);

                -- Chart-extraction results cache (issue #23)
                CREATE TABLE IF NOT EXISTS page_charts (
                    file_path                 TEXT    NOT NULL,
                    page_num                  INTEGER NOT NULL,
                    file_mtime                REAL    NOT NULL,
                    hints_hash                TEXT    NOT NULL,
                    max_points                INTEGER NOT NULL,
                    chart_extraction_version  INTEGER NOT NULL,
                    result_json               TEXT    NOT NULL,
                    created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_path, page_num, hints_hash, max_points)
                );

                CREATE INDEX IF NOT EXISTS idx_page_charts_path
                    ON page_charts(file_path);
            """)

            # page_charts: version-scoped invalidation — drop rows produced by
            # an older chart-extraction algorithm so stale results never leak
            # back out after a logic change (mirrors _EXTRACTION_VERSION for
            # page_text, but scoped to just this table).
            conn.execute(
                "DELETE FROM page_charts WHERE chart_extraction_version != ?",
                (CHART_EXTRACTION_VERSION,),
            )

            # page_text: add source column to existing tables (safe ALTER TABLE)
            cols = _get_columns(conn, "page_text")
            if cols and "source" not in cols:
                conn.execute(
                    "ALTER TABLE page_text ADD COLUMN source TEXT DEFAULT 'extracted'"
                )

            # page_text: record which language produced an OCR row, so a
            # request for a different language is a miss rather than a silent
            # hit on the first language ever used (issue #25). Rows written
            # before this column exist with NULL: unknown language, re-OCR once.
            if cols and "ocr_lang" not in cols:
                conn.execute(
                    "ALTER TABLE page_text ADD COLUMN ocr_lang TEXT DEFAULT NULL"
                )

            # page_text: widen the primary key to include ocr_lang so two
            # language spellings for one page stop evicting each other
            # (issue #27). Preserves every cached row.
            migrated_pk = _migrate_page_text_pk(conn)

            # pdf_metadata: add text_coverage_json column to existing tables
            cols = _get_columns(conn, "pdf_metadata")
            if cols and "text_coverage_json" not in cols:
                conn.execute(
                    "ALTER TABLE pdf_metadata"
                    " ADD COLUMN text_coverage_json TEXT DEFAULT NULL"
                )

            # pdf_metadata: content-trust scan cache (migrate old tables that
            # lack the column; trust_version column dropped — now global).
            cols = _get_columns(conn, "pdf_metadata")
            if cols and "content_trust_json" not in cols:
                conn.execute(
                    "ALTER TABLE pdf_metadata"
                    " ADD COLUMN content_trust_json TEXT DEFAULT NULL"
                )

            # page_text: per-page hidden-text flag (NULL = not yet computed)
            cols = _get_columns(conn, "page_text")
            if cols and "has_hidden_text" not in cols:
                conn.execute(
                    "ALTER TABLE page_text"
                    " ADD COLUMN has_hidden_text INTEGER DEFAULT NULL"
                )

            # Global content-trust invalidation via content_trust_meta.
            # On a fresh/never-stamped DB: record the current version (nothing
            # to wipe). When the stored version is below current: null both
            # caches and advance the stamp. Runs after page_text and
            # pdf_metadata tables exist.
            cur_tv = content_trust._TRUST_VERSION
            row = conn.execute(
                "SELECT trust_version FROM content_trust_meta WHERE id = 0"
            ).fetchone()
            stored_tv = row[0] if row else None
            if stored_tv is None:
                conn.execute(
                    "INSERT OR REPLACE INTO content_trust_meta (id, trust_version)"
                    " VALUES (0, ?)",
                    (cur_tv,),
                )
            elif stored_tv < cur_tv:
                conn.execute("UPDATE page_text SET has_hidden_text = NULL")
                conn.execute("UPDATE pdf_metadata SET content_trust_json = NULL")
                conn.execute(
                    "UPDATE content_trust_meta SET trust_version = ? WHERE id = 0",
                    (cur_tv,),
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

                cjk_existed = bool(
                    conn.execute(
                        "SELECT name FROM sqlite_master"
                        " WHERE type='table' AND name='pdf_search_fts_cjk'"
                    ).fetchone()
                )
                try:
                    conn.execute(_FTS5_CJK_TABLE_SCHEMA)
                    conn.execute(_FTS5_CJK_SECTION_TABLE_SCHEMA)
                except sqlite3.OperationalError:
                    # CJK tables failed but porter tables succeeded — leave
                    # fts_available=True; CJK queries degrade to no-match.
                    pass
                else:
                    if not cjk_existed and bool(_get_columns(conn, "page_text")):
                        self._backfill_cjk_tables(conn)

        # The PK migration's drop-and-rename leaves the freed pages in the
        # file (~50% growth, measured). VACUUM reclaims them and cannot run
        # inside a transaction, so it follows the commit above.
        if migrated_pk:
            with self._connect() as vac:
                vac.execute("VACUUM")

        # One line recording what this SQLite build actually gave us. No
        # minimum version is enforced (see server_info -> storage), and both
        # capabilities degrade silently, so a bug report about slow writes or
        # poor keyword ranking is otherwise indistinguishable from a corpus
        # problem.
        logger.debug(
            "SQLite %s at %s: journal_mode=%s, fts5=%s",
            sqlite3.sqlite_version,
            self.db_path,
            self.journal_mode,
            self.fts_available,
        )

        self.clear_expired()

    def _backfill_cjk_tables(self, conn: sqlite3.Connection) -> None:
        """One-time rebuild of CJK FTS tables from already-cached text.

        Reads original page_text and porter section rows; inserts char-split
        copies for CJK-containing rows only. No re-extraction, no re-embedding.
        """
        page_rows = conn.execute(
            "SELECT file_path, page_num, text FROM page_text"
        ).fetchall()
        page_inserts = [
            (fp, pn, _cjk_split(txt))
            for fp, pn, txt in page_rows
            if txt and _contains_cjk(txt)
        ]
        if page_inserts:
            conn.executemany(
                "INSERT INTO pdf_search_fts_cjk (file_path, page_num, text)"
                " VALUES (?, ?, ?)",
                page_inserts,
            )
        if bool(_get_columns(conn, "pdf_section_fts")):
            sec_rows = conn.execute(
                "SELECT file_path, section_id, title, text, start_page,"
                " end_page, title_source FROM pdf_section_fts"
            ).fetchall()
            sec_inserts = [
                (fp, sid, _cjk_split(title or ""), _cjk_split(text or ""), sp, ep, ts)
                for fp, sid, title, text, sp, ep, ts in sec_rows
                if _contains_cjk(title or "") or _contains_cjk(text or "")
            ]
            if sec_inserts:
                conn.executemany(
                    "INSERT INTO pdf_section_fts_cjk"
                    " (file_path, section_id, title, text,"
                    " start_page, end_page, title_source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    sec_inserts,
                )

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
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT file_mtime, file_size, page_count,
                   metadata, toc, text_coverage_json, content_trust_json
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
                "content_trust": (
                    json.loads(row["content_trust_json"])
                    if row["content_trust_json"]
                    else None
                ),
            }

    @contextlib.contextmanager
    def write_transaction(self) -> Any:
        """One connection, and so one commit, for a group of writes.

        Pass the yielded connection to each ``save_*`` call's ``conn``
        argument. See ``_write_conn`` for why this matters.
        """
        with self._connect() as conn:
            yield conn

    @contextlib.contextmanager
    def _write_conn(self, conn: "sqlite3.Connection | None" = None) -> Any:
        """Yield a connection, reusing the caller's if it owns a transaction.

        Every write here otherwise opens its own connection, and leaving
        that ``with`` block commits, which is an fsync. That is ~1ms on
        Linux and far more on Windows: warming a 6-document corpus spent
        3.18s of its 3.39s in commits there, against 0.05s on Linux, with
        extraction itself measured FASTER on Windows (0.205s vs 0.253s).
        Passing one connection through a document's writes turns four
        fsyncs into one, and keeps the document atomic as a bonus.
        """
        if conn is not None:
            yield conn
        else:
            with self._connect() as owned:
                yield owned

    def save_metadata(
        self,
        path: str,
        page_count: int,
        metadata: dict[str, Any],
        toc: list[Any],
        text_coverage: list[dict[str, Any]] | None = None,
        conn: "sqlite3.Connection | None" = None,
    ) -> None:
        """Save PDF metadata to cache, including optional text_coverage."""
        mtime, size = self._get_file_info(path)

        with self._write_conn(conn) as conn:
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

    def save_content_trust(self, path: str, scan: dict[str, Any]) -> None:
        """Persist the content-trust scan without touching other metadata."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE pdf_metadata SET content_trust_json = ? WHERE file_path = ?",
                (json.dumps(scan), path),
            )

    def get_content_trust(self, path: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content_trust_json, file_mtime FROM pdf_metadata"
                " WHERE file_path = ?",
                (path,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        if not self._is_cache_valid(path, row[1]):
            return None
        return cast(dict[str, Any], json.loads(row[0]))

    def save_page_blocks(
        self,
        path: str,
        blocks_by_page: "dict[int, tuple[list[Any], tuple[float, float]]]",
        conn: "sqlite3.Connection | None" = None,
    ) -> None:
        """Persist the sorted text-blocks shape per page (0-indexed).

        Written at warm time (and write-through on first live use) so the
        search excerpt path can build paragraph excerpts without touching
        the PDF at all. Keyed by mtime like page_text; dropped wholesale
        on an _EXTRACTION_VERSION bump because it derives from the same
        pipeline.
        """
        if not blocks_by_page:
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        with self._write_conn(conn) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS page_blocks (
                    file_path TEXT NOT NULL,
                    page_num INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    blocks TEXT NOT NULL,
                    page_size TEXT NOT NULL,
                    PRIMARY KEY (file_path, page_num)
                )""")
            conn.executemany(
                "INSERT OR REPLACE INTO page_blocks"
                " (file_path, page_num, mtime, blocks, page_size)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        path,
                        pn,
                        mtime,
                        json.dumps(blocks, ensure_ascii=False),
                        json.dumps(size),
                    )
                    for pn, (blocks, size) in blocks_by_page.items()
                ],
            )

    def get_page_blocks(
        self, path: str, page_num: int
    ) -> "tuple[list[Any], tuple[float, float]] | None":
        """Cached blocks + (width, height) for one page, or None."""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT blocks, page_size, mtime FROM page_blocks"
                    " WHERE file_path = ? AND page_num = ?",
                    (path, page_num),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None or abs(row[2] - mtime) > 1e-6:
            return None
        blocks = [tuple(b) for b in json.loads(row[0])]
        width, height = json.loads(row[1])
        return blocks, (float(width), float(height))

    def save_pages_hidden_flag(
        self,
        path: str,
        flags: dict[int, bool],
        conn: "sqlite3.Connection | None" = None,
    ) -> None:
        """Persist per-page hidden-text booleans (page_num is 0-indexed)."""
        if not flags:
            return
        with self._write_conn(conn) as conn:
            conn.executemany(
                "UPDATE page_text SET has_hidden_text = ?"
                " WHERE file_path = ? AND page_num = ?",
                [(1 if v else 0, path, pn) for pn, v in flags.items()],
            )

    def get_pages_hidden_flag(
        self, path: str, page_nums: list[int]
    ) -> dict[int, bool | None]:
        """Per-page flag: True/False if computed, None if not yet computed.
        Missing or stale pages are omitted."""
        if not page_nums:
            return {}
        placeholders = ",".join("?" * len(page_nums))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT page_num, has_hidden_text, file_mtime FROM page_text"
                f" WHERE file_path = ? AND page_num IN ({placeholders})",
                (path, *page_nums),
            ).fetchall()
        out: dict[int, bool | None] = {}
        for page_num, flag, mtime in rows:
            if not self._is_cache_valid(path, mtime):
                continue
            out[int(page_num)] = None if flag is None else bool(flag)
        return out

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
        with self._connect() as conn:
            row = conn.execute(
                _page_rows_query("page_num, text, file_mtime", 1, by_lang=False),
                (path, page_num),
            ).fetchone()

            if row is None:
                return None

            if not self._is_cache_valid(path, row[2]):
                return None

            return str(row[1])

    def get_pages_text(
        self, path: str, page_nums: list[int], ocr_lang: str | None = None
    ) -> dict[int, str]:
        """
        Get cached text for multiple pages.

        With ocr_lang, returns the row for that language (or the extracted
        row, which answers any language). Without it, returns the most
        recently written row per page.

        Args:
            path: Path to PDF file
            page_nums: List of page numbers (0-indexed)

        Returns:
            Dict mapping page_num to text for cached pages
        """
        if not page_nums:
            return {}

        by_lang = ocr_lang is not None
        query = _page_rows_query("page_num, text, file_mtime", len(page_nums), by_lang)
        params: tuple[object, ...] = (path, *page_nums)
        if by_lang:
            params = (*params, normalize_ocr_lang(ocr_lang))

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

            result = {}
            for page_num, text, mtime in rows:
                if self._is_cache_valid(path, mtime):
                    result[page_num] = text

            return result

    def save_page_text(
        self,
        path: str,
        page_num: int,
        text: str,
        source: str = "extracted",
        ocr_lang: str | None = None,
    ) -> None:
        """Save page text to cache with optional source label ('extracted' or
        'ocr') and, for OCR text, the Tesseract language that produced it.

        `source` stays coarse because it is a user-facing response field; the
        language lives in its own column so it can key the cache without
        leaking into responses.
        """
        mtime, _ = self._get_file_info(path)

        # Case and whitespace never change what Tesseract does, so they must
        # not create separate rows. Order IS preserved: it changes the output.
        # The caller's original string still goes to the -l flag (issue #27).
        stored_lang = normalize_ocr_lang(ocr_lang)

        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO page_text
                   (file_path, page_num, file_mtime,
                    text, text_length, source, ocr_lang)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (path, page_num, mtime, text, len(text), source, stored_lang),
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
        with self._connect() as conn:
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

    def get_pages_source(
        self, path: str, page_nums: list[int], ocr_lang: str | None = None
    ) -> dict[int, str]:
        """Bulk lookup of source for multiple pages. Missing/stale pages
        omitted. Row selection matches get_pages_text so the two never
        describe different rows of the same page."""
        if not page_nums:
            return {}
        by_lang = ocr_lang is not None
        query = _page_rows_query(
            "page_num, source, file_mtime", len(page_nums), by_lang
        )
        params: tuple[object, ...] = (path, *page_nums)
        if by_lang:
            params = (*params, normalize_ocr_lang(ocr_lang))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return {
            int(page_num): (str(source) if source else "extracted")
            for page_num, source, mtime in rows
            if self._is_cache_valid(path, mtime)
        }

    def get_pages_ocr_lang(
        self, path: str, page_nums: list[int], ocr_lang: str | None = None
    ) -> dict[int, str | None]:
        """Bulk lookup of the OCR language per page. Missing/stale pages are
        omitted; a row holding the '' sentinel maps to None (it is extracted
        text, or a legacy row whose language was never recorded, and either
        way no language describes it)."""
        if not page_nums:
            return {}
        by_lang = ocr_lang is not None
        query = _page_rows_query(
            "page_num, ocr_lang, file_mtime", len(page_nums), by_lang
        )
        params: tuple[object, ...] = (path, *page_nums)
        if by_lang:
            params = (*params, normalize_ocr_lang(ocr_lang))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return {
            int(page_num): (str(lang) if lang else None)
            for page_num, lang, mtime in rows
            if self._is_cache_valid(path, mtime)
        }

    def save_pages_text(
        self,
        path: str,
        pages: dict[int, str],
        conn: "sqlite3.Connection | None" = None,
    ) -> None:
        """
        Save multiple page texts to cache.

        Args:
            path: Path to PDF file
            pages: Dict mapping page_num to text
        """
        if not pages:
            return

        mtime, _ = self._get_file_info(path)

        with self._write_conn(conn) as conn:
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
                # CJK mirror, exactly as save_page_text maintains it. This
                # batch path skipped it, and the one-time backfill only runs
                # when the CJK tables are first created -- so a CJK document
                # warmed via pdf_corpus_warm had no rows in
                # pdf_search_fts_cjk and CJK keyword search found nothing in
                # it despite the text being cached.
                cjk = {pn: txt for pn, txt in pages.items() if _contains_cjk(txt)}
                if cjk:
                    cjk_nums = list(cjk.keys())
                    cjk_ph = ",".join("?" * len(cjk_nums))
                    conn.execute(
                        f"DELETE FROM pdf_search_fts_cjk"
                        f" WHERE file_path = ? AND page_num IN ({cjk_ph})",
                        (path, *cjk_nums),
                    )
                    conn.executemany(
                        "INSERT INTO pdf_search_fts_cjk"
                        " (file_path, page_num, text) VALUES (?, ?, ?)",
                        [(path, pn, _cjk_split(txt)) for pn, txt in cjk.items()],
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
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT image_index, width, height,
                   format, file_path_on_disk, size_bytes, file_mtime,
                   geometry_json
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

            result = []
            for row in real_rows:
                d = {
                    "page": page_num + 1,
                    "index": row["image_index"],
                    "width": row["width"],
                    "height": row["height"],
                    "format": row["format"],
                    "path": row["file_path_on_disk"],
                    "size_bytes": row["size_bytes"],
                }
                if row["geometry_json"]:
                    g = json.loads(row["geometry_json"])
                    d["bbox"] = g["bbox"]
                    if "placements" in g:
                        d["placements"] = g["placements"]
                result.append(d)
            return result

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

        with self._connect() as conn:
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
                    " file_path_on_disk, size_bytes, geometry_json)"
                    " VALUES (?, ?, -1, ?, 0, 0, 'sentinel',"
                    " '__sentinel__', 0, NULL)",
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
                    file_path_on_disk, size_bytes, geometry_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        (
                            json.dumps(
                                {
                                    "bbox": img["bbox"],
                                    **(
                                        {"placements": img["placements"]}
                                        if "placements" in img
                                        else {}
                                    ),
                                }
                            )
                            if "bbox" in img
                            else None
                        ),
                    )
                    for i, img in enumerate(images)
                ],
            )

    # ==================== Table Operations ====================

    def get_page_tables(self, path: str, page_num: int) -> list[dict[str, Any]] | None:
        """Get cached tables for a specific page. Returns None if not cached/invalid."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data, file_mtime FROM page_tables"
                " WHERE file_path = ? AND page_num = ?"
                " AND extraction_version = ?",
                (path, page_num, TABLE_EXTRACTION_VERSION),
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
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO page_tables"
                " (file_path, page_num, file_mtime, data, extraction_version)"
                " VALUES (?, ?, ?, ?, ?)",
                (path, page_num, mtime, json.dumps(tables), TABLE_EXTRACTION_VERSION),
            )

    # ==================== Embedding Operations ====================

    def get_page_embeddings(
        self, path: str, page_nums: list[int], model_name: str
    ) -> dict[int, list[bytes]]:
        """
        Get cached raw embedding bytes for multiple pages.

        Deletes any rows stored under a different model before querying,
        so the caller always gets embeddings consistent with model_name.

        Returns a dict mapping 0-indexed page_num to an ORDERED list of raw
        float32 blobs, one per sub-page chunk (ascending chunk_idx), for each
        page whose mtime is still valid. Pages not in cache or with a stale
        mtime are omitted.

        The caller is responsible for converting bytes to a numpy array:
            np.frombuffer(blob, dtype=np.float32).copy()

        Returns {} when page_nums is empty.
        """
        if not page_nums:
            return {}

        placeholders = ",".join("?" * len(page_nums))
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM page_embeddings WHERE file_path = ? AND model != ?",
                (path, model_name),
            )
            rows = conn.execute(
                f"SELECT page_num, chunk_idx, embedding, file_mtime"
                f" FROM page_embeddings"
                f" WHERE file_path = ? AND page_num IN ({placeholders})"
                f" AND model = ?"
                f" ORDER BY page_num, chunk_idx",
                (path, *page_nums, model_name),
            ).fetchall()

        result: dict[int, list[bytes]] = {}
        for page_num, _chunk_idx, blob, mtime in rows:
            if self._is_cache_valid(path, mtime):
                result.setdefault(int(page_num), []).append(bytes(blob))
        return result

    def save_page_embeddings(
        self,
        path: str,
        embeddings: dict[int, list[bytes]],
        model_name: str,
        conn: "sqlite3.Connection | None" = None,
    ) -> None:
        """
        Save raw embedding bytes to cache, one row per sub-page chunk.

        Args:
            path: Path to PDF file.
            embeddings: Dict mapping 0-indexed page_num to an ORDERED list of
                        raw float32 blobs, one per chunk. List position is
                        chunk_idx. An empty list stores nothing for that page.
            model_name: fastembed model identifier (stored alongside the blob).
        """
        if not embeddings:
            return

        mtime, _ = self._get_file_info(path)
        rows = [
            (path, page_num, idx, mtime, blob, model_name)
            for page_num, blobs in embeddings.items()
            for idx, blob in enumerate(blobs)
        ]
        if not rows:
            return
        with self._write_conn(conn) as conn:
            # Clear the page's existing chunks first: a page that shrinks from
            # three chunks to two would otherwise strand the third, because
            # INSERT OR REPLACE keys on (path, page, chunk_idx).
            conn.executemany(
                "DELETE FROM page_embeddings"
                " WHERE file_path = ? AND page_num = ? AND model = ?",
                [(path, page_num, model_name) for page_num in embeddings],
            )
            conn.executemany(
                "INSERT OR REPLACE INTO page_embeddings"
                " (file_path, page_num, chunk_idx, file_mtime, embedding, model)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def embeddings_complete(self, path: str, model_name: str) -> bool:
        """True when cached page text exists for `path` and every
        embedding-eligible cached page has an embedding for
        `model_name` (all rows mtime-valid).

        Eligibility MUST match the embedder's page predicate,
        ``text.strip()`` — the same one corpus warm's skip logic uses —
        or the two disagree forever on docs with whitespace-only pages
        (text_length > 0 but never embedded; real 368-page field
        sample). SQL narrows to unembedded text-bearing pages (zero
        rows on a healthy doc, so page text is normally never read),
        then Python ``strip()`` gives exact parity on the residue.

        Backs the corpus warm envelope's per-doc `embeddings_cached`
        field. Scoped to the pages currently cached; corpus warm
        callers only see it on docs that are fully text-warm.
        """
        try:
            mtime, _ = self._get_file_info(path)
        except OSError:
            return False
        with self._connect() as conn:
            (n_rows,) = conn.execute(
                "SELECT COUNT(*) FROM page_text"
                " WHERE file_path = ? AND file_mtime = ?",
                (path, mtime),
            ).fetchone()
            if int(n_rows) == 0:
                return False
            rows = conn.execute(
                "SELECT pt.text FROM page_text pt"
                " LEFT JOIN page_embeddings pe"
                "   ON pe.file_path = pt.file_path"
                "   AND pe.page_num = pt.page_num"
                "   AND pe.file_mtime = pt.file_mtime"
                "   AND pe.model = ?"
                " WHERE pt.file_path = ? AND pt.file_mtime = ?"
                "   AND pt.text_length > 0 AND pe.page_num IS NULL",
                (model_name, path, mtime),
            ).fetchall()
        return all(not (text or "").strip() for (text,) in rows)

    def save_doc_profile(
        self,
        path: str,
        head_chars: int,
        embedding: "bytes | None",
        terms: dict[str, int],
        model_name: str,
        conn: "sqlite3.Connection | None" = None,
    ) -> None:
        """Persist one document's profile (see corpus.build_doc_profile).

        ``embedding`` is None when page 1 carried no text; the row is
        still written so a backfill does not retry it forever. Keyed on
        file_path alone: a new model or mtime simply replaces the row.
        """
        mtime, _ = self._get_file_info(path)
        with self._write_conn(conn) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO doc_profiles"
                " (file_path, file_mtime, model, head_chars, embedding, terms)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (path, mtime, model_name, head_chars, embedding, json.dumps(terms)),
            )

    def _doc_profile_rows(self, paths: list[str]) -> list[tuple[Any, ...]]:
        """Raw doc_profiles rows for paths whose stored mtime matches the
        file's current mtime. Files that no longer exist are skipped."""
        if not paths:
            return []
        current: dict[str, float] = {}
        for p in paths:
            try:
                current[p] = self._get_file_info(p)[0]
            except OSError:
                continue
        if not current:
            return []
        out: list[tuple[Any, ...]] = []
        keys = list(current)
        with self._connect() as conn:
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT file_path, file_mtime, model, embedding, terms"
                    f" FROM doc_profiles WHERE file_path IN ({placeholders})",
                    chunk,
                ).fetchall()
                out.extend(r for r in rows if r[1] == current[r[0]])
        return out

    def get_doc_profiles(
        self, paths: list[str], model_name: str
    ) -> "dict[str, bytes | None]":
        """{path: head-vector blob or None} for valid (mtime, model) rows.

        None means "profiled, but page 1 had no text": the document has no
        vector and the caller should neither score nor re-encode it.
        """
        return {r[0]: r[3] for r in self._doc_profile_rows(paths) if r[2] == model_name}

    def get_doc_terms(self, paths: list[str]) -> dict[str, dict[str, int]]:
        """{path: {term: count}} for mtime-valid rows, any model."""
        return {r[0]: json.loads(r[4]) for r in self._doc_profile_rows(paths)}

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
        with self._connect() as conn:
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
        with self._connect() as conn:
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
        self,
        path: str,
        page_num: int,
        dpi: int,
        codec: str = "png",
        quality: int = 0,
    ) -> dict[str, Any] | None:
        """Get cached render for a page at a specific DPI, codec and quality.

        codec/quality are part of the key: a lossy JPEG must never satisfy a
        request for the lossless PNG at the same DPI.

        Returns None if not cached."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT file_path_on_disk, size_bytes, width, height,
                          file_mtime, codec, quality
                   FROM page_renders
                   WHERE file_path = ? AND page_num = ? AND dpi = ?
                     AND codec = ? AND quality = ?""",
                (path, page_num, dpi, codec, quality),
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
                "codec": row["codec"],
                "quality": row["quality"],
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

        Unlinks the old image if the path changed (orphan guard)."""
        codec = render_dict.get("codec", "png")
        quality = render_dict.get("quality", 0)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT file_path_on_disk FROM page_renders"
                " WHERE file_path = ? AND page_num = ? AND dpi = ?"
                " AND codec = ? AND quality = ?",
                (path, page_num, dpi, codec, quality),
            ).fetchone()
            if existing and existing[0] != render_dict["file_path_on_disk"]:
                try:
                    Path(existing[0]).unlink()
                except FileNotFoundError:
                    pass
            conn.execute(
                """INSERT OR REPLACE INTO page_renders
                   (file_path, page_num, file_mtime, dpi, codec, quality,
                    file_path_on_disk, size_bytes, width, height)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    path,
                    page_num,
                    file_mtime,
                    dpi,
                    codec,
                    quality,
                    render_dict["file_path_on_disk"],
                    render_dict["size_bytes"],
                    render_dict["width"],
                    render_dict["height"],
                ),
            )

    # ==================== Chart Extraction Operations ====================

    def get_page_charts(
        self, path: str, page_num: int, hints_hash: str, max_points: int
    ) -> dict[str, Any] | None:
        """Get cached chart-extraction result for a page.

        Keyed on (path, page_num, hints_hash, max_points). Returns None if
        not cached, or if the source file's mtime has changed since caching.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                # version filter on READ, not just the purge at open: with
                # per-conversation STDIO servers, an older-code process can
                # re-populate old-version rows AFTER this process's open-time
                # purge — without the filter those rows would be served as
                # current.
                """SELECT result_json, file_mtime
                   FROM page_charts
                   WHERE file_path = ? AND page_num = ?
                     AND hints_hash = ? AND max_points = ?
                     AND chart_extraction_version = ?""",
                (path, page_num, hints_hash, max_points, CHART_EXTRACTION_VERSION),
            ).fetchone()
            if row is None:
                return None
            if not self._is_cache_valid(path, row["file_mtime"]):
                return None
            result: dict[str, Any] = json.loads(row["result_json"])
            return result

    def save_page_charts(
        self,
        path: str,
        page_num: int,
        hints_hash: str,
        max_points: int,
        result: dict[str, Any],
    ) -> None:
        """Save a chart-extraction result to cache."""
        mtime, _ = self._get_file_info(path)

        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO page_charts
                   (file_path, page_num, file_mtime, hints_hash, max_points,
                    chart_extraction_version, result_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    path,
                    page_num,
                    mtime,
                    hints_hash,
                    max_points,
                    CHART_EXTRACTION_VERSION,
                    json.dumps(result),
                ),
            )

    # ==================== Cache Management ====================

    def _invalidate_file(self, path: str) -> None:
        """Remove all cache entries for a file."""
        with self._connect() as conn:
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
            conn.execute("DELETE FROM page_charts WHERE file_path = ?", (path,))

            conn.execute("DELETE FROM pdf_metadata WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM page_text WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM page_images WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM page_tables WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM page_embeddings WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM doc_profiles WHERE file_path = ?", (path,))
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
        with self._connect() as conn:
            # Get expired file paths. Compute the cutoff with SQLite's own
            # clock — accessed_at is written via CURRENT_TIMESTAMP (UTC,
            # "YYYY-MM-DD HH:MM:SS"), so comparing it against a Python
            # datetime.now().isoformat() (local timezone, "T" separator)
            # mis-sorts fresh rows as expired. datetime('now', ?) keeps both
            # sides in the same clock and format.
            expired = conn.execute(
                "SELECT file_path FROM pdf_metadata"
                " WHERE accessed_at < datetime('now', ?)",
                (f"-{self.ttl_hours} hours",),
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
                    f"DELETE FROM doc_profiles WHERE file_path IN ({placeholders})",
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
                conn.execute(
                    f"DELETE FROM page_charts WHERE file_path IN ({placeholders})",
                    expired_paths,
                )

        # Sweep page_renders for stale-mtime entries (PDF file changed)
        with self._connect() as conn2:
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

        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM pdf_metadata").fetchone()[0]
            conn.execute("DELETE FROM pdf_metadata")
            conn.execute("DELETE FROM page_text")
            conn.execute("DELETE FROM page_images")
            conn.execute("DELETE FROM page_tables")
            conn.execute("DELETE FROM page_embeddings")
            conn.execute("DELETE FROM doc_profiles")
            conn.execute("DELETE FROM section_embeddings")
            conn.execute("DELETE FROM page_renders")
            conn.execute("DELETE FROM page_charts")
            if self.fts_available:
                conn.execute("DELETE FROM pdf_search_fts")
                conn.execute("DELETE FROM pdf_search_fts_cjk")
                conn.execute("DELETE FROM pdf_section_fts")
                conn.execute("DELETE FROM pdf_section_fts_cjk")
            # Return freed pages to the filesystem, or the DB file keeps
            # its high-water size and cache_size_bytes reports megabytes
            # of residual after a full clear. VACUUM cannot run inside a
            # transaction, so commit the deletes first.
            conn.commit()
            conn.execute("VACUUM")
            # In WAL mode VACUUM's rebuilt pages land in the -wal sidecar, so
            # the main database file keeps its high-water size until a
            # checkpoint folds them back in. TRUNCATE also resets the sidecar
            # itself, so cache_size_bytes reports the real post-clear size
            # instead of megabytes of residual.
            #
            # Suppressed: the TRUNCATE argument needs SQLite 3.8.8, and this
            # project declares no minimum SQLite version (requires-python is
            # the only floor). It also no-ops while another connection reads.
            # Either way a checkpoint is an optimisation and must not be able
            # to fail a cache clear.
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return int(count)

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with self._connect() as conn:
            stats = {}

            # Count files
            stats["total_files"] = conn.execute(
                "SELECT COUNT(*) FROM pdf_metadata"
            ).fetchone()[0]

            # Count pages, not rows: one page can hold several ocr_lang rows.
            stats["total_pages"] = conn.execute(
                "SELECT COUNT(*) FROM"
                " (SELECT DISTINCT file_path, page_num FROM page_text)"
            ).fetchone()[0]

            # Count images (exclude sentinel rows)
            stats["total_images"] = conn.execute(
                "SELECT COUNT(*) FROM page_images WHERE image_index >= 0"
            ).fetchone()[0]

            stats["total_tables"] = conn.execute(
                "SELECT COALESCE(SUM(json_array_length(data)), 0) FROM page_tables"
            ).fetchone()[0]

            stats["embedding_pages"] = conn.execute(
                "SELECT COUNT(DISTINCT file_path || ':' || page_num)"
                " FROM page_embeddings"
            ).fetchone()[0]

            stats["total_renders"] = conn.execute(
                "SELECT COUNT(*) FROM page_renders"
            ).fetchone()[0]

            stats["total_charts"] = conn.execute(
                "SELECT COUNT(*) FROM page_charts"
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
                # "*" not "*.png": render_pages also writes ".jpg" files
                # (the JPEG fallback ladder), which "*.png" silently missed.
                renders_size = sum(
                    f.stat().st_size for f in self.renders_dir.glob("*") if f.is_file()
                )
            except FileNotFoundError:
                renders_size = 0
            # Fold the WAL back into the main database before sizing it.
            # Summing cache.db* instead would report a number that moves with
            # checkpoint timing: the -wal sidecar grows as pages are written
            # and drops back on every auto-checkpoint, so the reported cache
            # size could FALL right after a caller added a document. This is
            # a diagnostic call, so paying a checkpoint here buys a stable,
            # monotonic number. TRUNCATE is best-effort: it no-ops while
            # another connection is reading, which only leaves some bytes in
            # the sidecar, so both files are still counted below.
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db_size = sum(
                f.stat().st_size
                for f in self.cache_dir.glob(self.db_path.name + "*")
                if f.is_file()
            )
            stats["cache_size_bytes"] = db_size + images_size + renders_size
            stats["cache_size_mb"] = round(stats["cache_size_bytes"] / (1024 * 1024), 2)

            return stats

    # ==================== FTS5 Search Operations ====================

    def _cjk_excerpt(
        self, path: str, page_num: int, query: str, context_chars: int
    ) -> str | None:
        """Build an excerpt from ORIGINAL page text for a CJK match.

        Query tokens (whitespace-split, AND semantics matching the FTS5 query)
        are checked independently: EACH token must appear as a literal
        substring of the page text. This is the contiguity post-filter that
        drops rare cross-separator false positives, applied per token rather
        than to the whole query, so a multi-term query is not required to
        appear as one contiguous run. Returns None when any token's literal
        substring is absent. Otherwise returns a context window centered on
        the earliest-occurring token (lowest string index) among those found.
        """
        text = self.get_page_text(path, page_num) or ""
        tokens = query.split()
        if not tokens:
            return None
        best_idx: int | None = None
        best_needle = ""
        for token in tokens:
            idx = text.find(token)
            if idx < 0:
                return None
            if best_idx is None or idx < best_idx:
                best_idx = idx
                best_needle = token
        assert best_idx is not None
        half = max(0, (context_chars - len(best_needle)) // 2)
        start = max(0, best_idx - half)
        end = min(len(text), best_idx + len(best_needle) + half)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(text) else ""
        return f"{prefix}{text[start:end]}{suffix}"

    def _build_temp_page_fts(
        self, conn: sqlite3.Connection, path: str, cjk: bool
    ) -> None:
        """Build a connection-local FTS index over one document's pages.

        FTS5 ``bm25()`` derives IDF from term statistics over the ENTIRE
        virtual table, so ranking against the shared ``pdf_search_fts`` table
        depends on every other cached PDF (issue #17). Rebuilding a temp index
        holding only this document's pages makes ``bm25()`` IDF document-local,
        so a PDF's page ranking is stable regardless of what else is cached.
        The temp table is dropped automatically when ``conn`` closes.
        """
        tokenizer = "unicode61" if cjk else "porter unicode61"
        conn.execute("DROP TABLE IF EXISTS temp.doc_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE temp.doc_fts USING fts5("
            f"page_num UNINDEXED, text, tokenize='{tokenizer}')"
        )
        if cjk:
            rows = conn.execute(
                "SELECT page_num, text FROM page_text WHERE file_path = ?",
                (path,),
            ).fetchall()
            conn.executemany(
                "INSERT INTO temp.doc_fts (page_num, text) VALUES (?, ?)",
                [(pn, _cjk_split(txt)) for pn, txt in rows],
            )
        else:
            conn.execute(
                "INSERT INTO temp.doc_fts (page_num, text)"
                " SELECT page_num, text FROM page_text WHERE file_path = ?",
                (path,),
            )

    def search_fts(
        self,
        path: str,
        query: str,
        max_results: int,
        context_chars: int,
        allow_or_fallback: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Search the FTS5 index for pages matching query.

        Returns at most max_results results sorted by descending BM25 relevance.
        Each result has keys: page (1-indexed), excerpt (str), score (float >= 0).
        Returns [] when fts_available is False or no matches found.

        Args:
            path: Path to PDF file (must match the value stored at index time)
            query: Search query (Porter stemming applied; FTS5 operators escaped)
            allow_or_fallback: retry an unmatched multi-word query with the
                tokens OR-joined (see `_fts5_or_fallback`). Callers that
                search MANY documents and compare them must pass False:
                relaxing every document independently floods the comparison
                with loose single-term hits and destroys the discrimination
                that "this document matched and that one did not" provides.
            max_results: Maximum number of results to return
            context_chars: Approximate characters of context in excerpts
        """
        if not self.fts_available:
            return []

        if _contains_cjk(query):
            escaped = _escape_fts5_query_cjk(query)
            with self._connect() as conn:
                try:
                    self._build_temp_page_fts(conn, path, cjk=True)
                    rows = conn.execute(
                        "SELECT page_num, -bm25(doc_fts)"
                        " FROM doc_fts"
                        " WHERE doc_fts MATCH ?"
                        " ORDER BY bm25(doc_fts) LIMIT ?",
                        (escaped, max_results),
                    ).fetchall()
                except sqlite3.OperationalError:
                    return []
            out: list[dict[str, Any]] = []
            for page_num, score in rows:
                excerpt = self._cjk_excerpt(path, int(page_num), query, context_chars)
                if excerpt is None:
                    continue  # contiguity post-filter: cross-separator false hit
                out.append(
                    {
                        "page": int(page_num) + 1,
                        "excerpt": excerpt,
                        "score": float(score),
                    }
                )
            return out

        escaped = _escape_fts5_query(query)
        # Map context_chars to FTS5 snippet token count (approximate)
        num_tokens = max(4, min(64, context_chars // 5))

        with self._connect() as conn:
            try:
                self._build_temp_page_fts(conn, path, cjk=False)
                sql = (
                    "SELECT page_num,"
                    " snippet(doc_fts, 1, '', '', '...', ?),"
                    " -bm25(doc_fts)"
                    " FROM doc_fts"
                    " WHERE doc_fts MATCH ?"
                    " ORDER BY bm25(doc_fts)"
                    " LIMIT ?"
                )
                rows = conn.execute(sql, (num_tokens, escaped, max_results)).fetchall()
                if not rows and allow_or_fallback:
                    # Every AND token must share a page; one absent word
                    # zeroes an otherwise precise question-shaped query.
                    alt = _fts5_or_fallback(query)
                    if alt is not None:
                        rows = conn.execute(
                            sql, (num_tokens, alt, max_results)
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

        if _contains_cjk(query):
            escaped = _escape_fts5_query_cjk(query)
            needle = "".join(query.split())
            with self._connect() as conn:
                try:
                    rows = conn.execute(
                        "SELECT page_num FROM pdf_search_fts_cjk"
                        " WHERE pdf_search_fts_cjk MATCH ? AND file_path = ?",
                        (escaped, path),
                    ).fetchall()
                except sqlite3.OperationalError:
                    return {}
            counts: dict[int, int] = {}
            for (page_num,) in rows:
                text = self.get_page_text(path, int(page_num)) or ""
                count = text.count(needle)
                if count > 0:
                    counts[int(page_num)] = count
            return counts

        tokens_lower = [_FTS_TOKEN_STRIP.sub("", tok).lower() for tok in query.split()]
        tokens_lower = [t for t in tokens_lower if t]
        if not tokens_lower:
            return {}

        escaped = _escape_fts5_query(query)

        with self._connect() as conn:
            try:
                sql = (
                    "SELECT page_num, text"
                    " FROM pdf_search_fts"
                    " WHERE pdf_search_fts MATCH ? AND file_path = ?"
                )
                rows = conn.execute(sql, (escaped, path)).fetchall()
                if not rows:
                    # Mirror search_fts's fallback so the documented
                    # invariant holds: every page in `matches` appears here.
                    alt = _fts5_or_fallback(query)
                    if alt is not None:
                        rows = conn.execute(sql, (alt, path)).fetchall()
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
        with self._connect() as conn:
            # DISTINCT page_num: a page holds one row per ocr_lang, but FTS
            # holds one row per page, so counting rows here would make the
            # `indexed == total == doc_pages` check fail on any document with
            # a page cached in two languages and silently drop it onto the
            # slower per-query index (issue #27).
            total = conn.execute(
                "SELECT COUNT(DISTINCT page_num) FROM page_text" " WHERE file_path = ?",
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
        with self._connect() as conn:
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

    def _build_temp_section_fts(
        self, conn: sqlite3.Connection, path: str, cjk: bool
    ) -> None:
        """Build a connection-local section FTS index over one document.

        Same document-local IDF rationale as ``_build_temp_page_fts`` (issue
        #17). Rows are copied from the shared section table (already
        tokenized at index time) for this ``path`` only. Dropped when
        ``conn`` closes.
        """
        src = "pdf_section_fts_cjk" if cjk else "pdf_section_fts"
        tokenizer = "unicode61" if cjk else "porter unicode61"
        conn.execute("DROP TABLE IF EXISTS temp.doc_sec_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE temp.doc_sec_fts USING fts5("
            "section_id UNINDEXED, title, text,"
            " start_page UNINDEXED, end_page UNINDEXED,"
            f" title_source UNINDEXED, tokenize='{tokenizer}')"
        )
        conn.execute(
            "INSERT INTO temp.doc_sec_fts"
            " (section_id, title, text, start_page, end_page, title_source)"
            " SELECT section_id, title, text, start_page, end_page,"
            f" title_source FROM {src} WHERE file_path = ?",
            (path,),
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
        if _contains_cjk(query):
            escaped = _escape_fts5_query_cjk(query)
            with self._connect() as conn:
                try:
                    self._build_temp_section_fts(conn, path, cjk=True)
                    rows = conn.execute(
                        "SELECT section_id, title, start_page, end_page,"
                        " title_source, -bm25(doc_sec_fts)"
                        " FROM doc_sec_fts"
                        " WHERE doc_sec_fts MATCH ?"
                        " ORDER BY bm25(doc_sec_fts)"
                        " LIMIT ?",
                        (escaped, max_results),
                    ).fetchall()
                except sqlite3.OperationalError:
                    return []
                # Restore original (unsplit) titles from the porter section
                # table for clean display — unchanged from prior behavior.
                orig = conn.execute(
                    "SELECT section_id, title FROM pdf_section_fts"
                    " WHERE file_path = ?",
                    (path,),
                ).fetchall()
                title_by_id = {int(s): t for s, t in orig}
            return [
                {
                    "section_id": int(sid),
                    "title": title_by_id.get(int(sid), title),
                    "start_page": int(sp),
                    "end_page": int(ep),
                    "title_source": title_source,
                    "score": float(score),
                }
                for sid, title, sp, ep, title_source, score in rows
            ]
        escaped = _escape_fts5_query(query)
        with self._connect() as conn:
            try:
                self._build_temp_section_fts(conn, path, cjk=False)
                sql = (
                    "SELECT section_id, title, start_page, end_page,"
                    " title_source, -bm25(doc_sec_fts)"
                    " FROM doc_sec_fts"
                    " WHERE doc_sec_fts MATCH ?"
                    " ORDER BY bm25(doc_sec_fts)"
                    " LIMIT ?"
                )
                rows = conn.execute(sql, (escaped, max_results)).fetchall()
                if not rows:
                    alt = _fts5_or_fallback(query)
                    if alt is not None:
                        rows = conn.execute(sql, (alt, max_results)).fetchall()
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
        with self._connect() as conn:
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
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT file_mtime FROM section_embeddings WHERE file_path = ?",
                (path,),
            ).fetchall()
        return sum(1 for (mtime,) in rows if self._is_cache_valid(path, mtime))
