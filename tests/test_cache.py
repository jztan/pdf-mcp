# tests/test_cache.py
"""Tests for pdf_mcp.cache module - edge cases."""

import os
import sqlite3
import time
import tempfile
from pathlib import Path

import pytest

from pdf_mcp.cache import PDFCache, _get_columns


@pytest.fixture
def cache_with_data(cache, sample_pdf):
    """Cache pre-populated with test data."""
    cache.save_metadata(sample_pdf, 5, {"title": "Test"}, [])
    cache.save_page_text(sample_pdf, 0, "Page 1 content")
    cache.save_page_text(sample_pdf, 1, "Page 2 content")
    return cache, sample_pdf


class TestCacheValidation:
    """Tests for cache validation edge cases."""

    def test_is_cache_valid_file_deleted(self, cache):
        """Deleted file returns False for cache validity."""
        # Create temp file, cache it, then delete
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4")
            temp_path = f.name

        # Get mtime before deletion
        mtime = os.stat(temp_path).st_mtime

        # Delete the file
        os.unlink(temp_path)

        # _is_cache_valid should return False (OSError)
        result = cache._is_cache_valid(temp_path, mtime)
        assert result is False

    def test_get_metadata_invalidates_on_mtime_change(self, cache, sample_pdf):
        """Changed file mtime invalidates cached metadata."""
        # Save metadata
        cache.save_metadata(sample_pdf, 5, {"title": "Test"}, [])

        # Verify it's cached
        assert cache.get_metadata(sample_pdf) is not None

        # Touch the file to change mtime
        time.sleep(0.1)
        Path(sample_pdf).touch()

        # Should return None and invalidate
        result = cache.get_metadata(sample_pdf)
        assert result is None

    def test_get_page_text_invalid_mtime(self, cache, sample_pdf):
        """Page text with wrong mtime returns None."""
        cache.save_page_text(sample_pdf, 0, "Content")

        # Touch file to change mtime
        time.sleep(0.1)
        Path(sample_pdf).touch()

        result = cache.get_page_text(sample_pdf, 0)
        assert result is None

    def test_get_page_images_invalid_mtime(self, cache, sample_pdf):
        """Page images with wrong mtime returns None."""
        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 100,
                    "height": 100,
                    "format": "rgb",
                    "path": "/tmp/test.png",
                    "size_bytes": 100,
                }
            ],
        )

        # Touch file
        time.sleep(0.1)
        Path(sample_pdf).touch()

        result = cache.get_page_images(sample_pdf, 0)
        assert result is None


class TestEmptyInputs:
    """Tests for empty input handling."""

    def test_get_pages_text_empty_list(self, cache, sample_pdf):
        """Empty page list returns empty dict."""
        result = cache.get_pages_text(sample_pdf, [])
        assert result == {}

    def test_save_pages_text_empty_dict(self, cache, sample_pdf):
        """Empty pages dict is a no-op."""
        # Should not raise
        cache.save_pages_text(sample_pdf, {})

        # Verify nothing was saved
        stats = cache.get_stats()
        assert stats["total_pages"] == 0


class TestCacheInvalidation:
    """Tests for cache invalidation."""

    def test_invalidate_file_clears_all_tables(self, cache_with_data):
        """_invalidate_file removes data from all tables."""
        cache, sample_pdf = cache_with_data

        # Add images too
        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 10,
                    "height": 10,
                    "format": "rgb",
                    "path": "/tmp/test.png",
                    "size_bytes": 100,
                }
            ],
        )

        # Verify data exists
        assert cache.get_metadata(sample_pdf) is not None

        # Manually invalidate
        cache._invalidate_file(sample_pdf)

        # All data should be gone
        stats = cache.get_stats()
        assert stats["total_files"] == 0
        assert stats["total_pages"] == 0
        assert stats["total_images"] == 0

    def test_get_page_images_returns_path(self, cache, sample_pdf, tmp_path):
        """Cached images return path and size_bytes, not base64 data."""
        img_file = tmp_path / "test.png"
        img_file.write_bytes(b"\x89PNG fake")

        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 100,
                    "height": 100,
                    "format": "rgb",
                    "path": str(img_file),
                    "size_bytes": 9,
                }
            ],
        )
        result = cache.get_page_images(sample_pdf, 0)
        assert result is not None
        assert len(result) == 1
        assert "path" in result[0]
        assert "size_bytes" in result[0]
        assert result[0]["size_bytes"] == 9
        assert "data" not in result[0]
        assert result[0]["path"] == str(img_file)

    def test_get_page_images_cache_miss_when_file_missing(self, cache, sample_pdf):
        """DB row exists but PNG file missing on disk -> cache miss (None)."""
        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 100,
                    "height": 100,
                    "format": "rgb",
                    "path": "/nonexistent/deleted.png",
                    "size_bytes": 100,
                }
            ],
        )
        result = cache.get_page_images(sample_pdf, 0)
        assert result is None

    def test_get_stats_includes_image_dir_size(self, cache, tmp_path):
        """get_stats reports combined DB + image directory size."""
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        # Create a fake image file
        (images_dir / "test.png").write_bytes(b"x" * 1000)

        cache.images_dir = images_dir
        stats = cache.get_stats()
        # Should include the 1000-byte file
        assert stats["cache_size_bytes"] >= 1000

    def test_clear_all_deletes_image_files(self, cache, tmp_path):
        """clear_all() removes all PNG files from images directory."""
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        (images_dir / "abc_p0_i0.png").write_bytes(b"\x89PNG")
        (images_dir / "def_p1_i0.png").write_bytes(b"\x89PNG")
        cache.images_dir = images_dir

        cache.clear_all()

        assert not any(images_dir.iterdir())  # dir exists but empty

    def test_invalidate_file_deletes_image_files(self, cache, sample_pdf, tmp_path):
        """_invalidate_file() deletes PNGs for that file."""
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        png = images_dir / "abc_p0_i0.png"
        png.write_bytes(b"\x89PNG")
        cache.images_dir = images_dir

        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 10,
                    "height": 10,
                    "format": "rgb",
                    "path": str(png),
                    "size_bytes": 4,
                }
            ],
        )
        cache._invalidate_file(sample_pdf)

        assert not png.exists()

    def test_clear_expired_deletes_image_files(self, cache, sample_pdf, tmp_path):
        """clear_expired() deletes PNGs for expired entries."""
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        png = images_dir / "abc_p0_i0.png"
        png.write_bytes(b"\x89PNG")
        cache.images_dir = images_dir

        # Save with very short TTL cache (already ttl_hours=1 in fixture)
        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 10,
                    "height": 10,
                    "format": "rgb",
                    "path": str(png),
                    "size_bytes": 4,
                }
            ],
        )
        cache.save_metadata(sample_pdf, 1, {}, [])

        # Manually backdate the accessed_at to force expiration
        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            conn.execute("UPDATE pdf_metadata SET accessed_at = '2020-01-01T00:00:00'")

        cache.clear_expired()
        assert not png.exists()

    def test_init_clears_expired_on_startup(self, tmp_path):
        """New PDFCache instance prunes expired entries on init."""
        cache1 = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        images_dir = tmp_path / "images"
        images_dir.mkdir(exist_ok=True)
        png = images_dir / "old_p0_i0.png"
        png.write_bytes(b"\x89PNG")

        # Create sample PDF for metadata
        import tempfile

        import pymupdf

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            doc = pymupdf.open()
            doc.new_page()
            doc.save(f.name)
            doc.close()
            pdf_path = f.name

        cache1.save_metadata(pdf_path, 1, {}, [])
        cache1.save_page_images(
            pdf_path,
            0,
            [
                {
                    "index": 0,
                    "width": 10,
                    "height": 10,
                    "format": "rgb",
                    "path": str(png),
                    "size_bytes": 4,
                }
            ],
        )

        # Backdate to force expiry
        import sqlite3

        with sqlite3.connect(cache1.db_path) as conn:
            conn.execute("UPDATE pdf_metadata SET accessed_at = '2020-01-01'")

        # New cache instance should auto-prune on init
        cache2 = PDFCache(cache_dir=tmp_path, ttl_hours=1)

        stats = cache2.get_stats()
        assert stats["total_files"] == 0
        assert not png.exists()

        os.unlink(pdf_path)

    def test_save_page_images_cleans_stale_files(self, cache, sample_pdf, tmp_path):
        """Re-saving images for a page deletes old PNGs first."""
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        cache.images_dir = images_dir

        png0 = images_dir / "abc_p0_i0.png"
        png1 = images_dir / "abc_p0_i1.png"
        png0.write_bytes(b"\x89PNG img0")
        png1.write_bytes(b"\x89PNG img1")

        # Initial save with 2 images -> creates DB rows with file paths
        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 10,
                    "height": 10,
                    "format": "rgb",
                    "path": str(png0),
                    "size_bytes": 9,
                },
                {
                    "index": 1,
                    "width": 10,
                    "height": 10,
                    "format": "rgb",
                    "path": str(png1),
                    "size_bytes": 9,
                },
            ],
        )

        # Re-save with only 1 image — old DB rows queried, old files deleted
        new_png = images_dir / "abc_p0_i0.png"
        new_png.write_bytes(b"\x89PNG new")
        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 10,
                    "height": 10,
                    "format": "rgb",
                    "path": str(new_png),
                    "size_bytes": 8,
                },
            ],
        )

        # Old orphan i1 should be deleted
        assert not png1.exists()

    def test_mtime_change_invalidates_on_access(self, cache, sample_pdf):
        """Accessing stale cache triggers invalidation."""
        cache.save_metadata(sample_pdf, 5, {}, [])
        cache.save_page_text(sample_pdf, 0, "Content")

        # Change file
        time.sleep(0.1)
        Path(sample_pdf).touch()

        # Access triggers invalidation
        cache.get_metadata(sample_pdf)

        # Metadata should be cleared (though page_text cleanup is separate)
        stats = cache.get_stats()
        assert stats["total_files"] == 0


class TestCacheSentinel:
    """Tests for sentinel caching of imageless pages."""

    def test_save_page_images_empty_inserts_sentinel(self, cache, sample_pdf):
        """Saving empty image list inserts a sentinel row (image_index=-1)."""
        cache.save_page_images(sample_pdf, 0, [])

        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            rows = conn.execute(
                "SELECT image_index FROM page_images"
                " WHERE file_path = ? AND page_num = ?",
                (sample_pdf, 0),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == -1  # sentinel

    def test_get_page_images_returns_empty_for_sentinel(self, cache, sample_pdf):
        """get_page_images returns [] (not None) when sentinel exists."""
        cache.save_page_images(sample_pdf, 0, [])
        result = cache.get_page_images(sample_pdf, 0)
        assert result == []  # Currently returns None — this will fail

    def test_get_page_images_returns_none_for_unchecked(self, cache, sample_pdf):
        """get_page_images returns None when page has never been checked."""
        result = cache.get_page_images(sample_pdf, 0)
        assert result is None

    def test_get_stats_excludes_sentinel_rows(self, cache, sample_pdf):
        """get_stats counts only real images, not sentinel rows."""
        cache.save_page_images(sample_pdf, 0, [])  # sentinel only
        stats = cache.get_stats()
        assert stats["total_images"] == 0

    def test_save_page_images_empty_replaces_existing_rows(
        self, cache, sample_pdf, tmp_path
    ):
        """Saving empty list after real images deletes old rows and inserts sentinel."""
        img_file = tmp_path / "test.png"
        img_file.write_bytes(b"\x89PNG fake")

        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 100,
                    "height": 100,
                    "format": "rgb",
                    "path": str(img_file),
                    "size_bytes": 9,
                },
            ],
        )

        # Now save empty → should replace with sentinel
        cache.save_page_images(sample_pdf, 0, [])

        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            rows = conn.execute(
                "SELECT image_index FROM page_images"
                " WHERE file_path = ? AND page_num = ?",
                (sample_pdf, 0),
            ).fetchall()
        # Only sentinel remains, old row deleted
        assert len(rows) == 1
        assert rows[0][0] == -1

        # Old PNG cleaned up from disk
        assert not img_file.exists()


class TestCacheCoverageEdgeCases:
    """Tests for uncovered edge-case lines in cache.py."""

    def test_db_migration_drops_old_page_images_with_data_column(self, tmp_path):
        """L56: Old schema with 'data' column triggers DROP TABLE and re-creation."""
        import sqlite3

        db_path = tmp_path / "cache.db"

        # Create an old-schema page_images table with a 'data' BLOB column
        with sqlite3.connect(db_path) as conn:
            conn.execute("""CREATE TABLE page_images (
                    file_path TEXT NOT NULL,
                    page_num INTEGER NOT NULL,
                    image_index INTEGER NOT NULL,
                    file_mtime REAL NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    format TEXT NOT NULL,
                    data BLOB NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_path, page_num, image_index)
                )""")

        # Instantiate PDFCache — should detect old schema and recreate
        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)

        # Verify new schema has file_path_on_disk and no data column
        with sqlite3.connect(cache.db_path) as conn:
            cursor = conn.execute("PRAGMA table_info(page_images)")
            columns = {row[1] for row in cursor.fetchall()}

        assert "file_path_on_disk" in columns
        assert "data" not in columns

    def test_sentinel_save_handles_already_deleted_file(
        self, cache, sample_pdf, tmp_path
    ):
        """L369-370: save_page_images(path, 0, []) handles FileNotFoundError
        when old image file is already deleted from disk."""
        # Save an initial image whose file_path_on_disk doesn't exist on disk
        nonexistent = str(tmp_path / "already_deleted.png")
        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 100,
                    "height": 100,
                    "format": "rgb",
                    "path": nonexistent,
                    "size_bytes": 100,
                }
            ],
        )

        # Now save empty list — cleanup loop tries to unlink nonexistent file
        cache.save_page_images(sample_pdf, 0, [])

        # Verify sentinel was inserted successfully
        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            rows = conn.execute(
                "SELECT image_index, file_path_on_disk FROM page_images"
                " WHERE file_path = ? AND page_num = ?",
                (sample_pdf, 0),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == -1  # sentinel
        assert rows[0][1] == "__sentinel__"

    def test_orphan_cleanup_handles_already_deleted_file(
        self, cache, sample_pdf, tmp_path
    ):
        """L400-401: save_page_images with new images handles FileNotFoundError
        when orphan file is already deleted from disk."""
        # Save an initial image whose file_path_on_disk doesn't exist on disk
        nonexistent = str(tmp_path / "orphan_gone.png")
        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 100,
                    "height": 100,
                    "format": "rgb",
                    "path": nonexistent,
                    "size_bytes": 100,
                }
            ],
        )

        # Save a different set of images — orphan cleanup tries to unlink
        # nonexistent, which raises FileNotFoundError (caught silently)
        new_img = tmp_path / "new_image.png"
        new_img.write_bytes(b"\x89PNG new data")
        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 200,
                    "height": 200,
                    "format": "rgb",
                    "path": str(new_img),
                    "size_bytes": 13,
                }
            ],
        )

        # Verify new image was saved correctly
        import sqlite3

        with sqlite3.connect(cache.db_path) as conn:
            rows = conn.execute(
                "SELECT file_path_on_disk, width, size_bytes FROM page_images"
                " WHERE file_path = ? AND page_num = ?",
                (sample_pdf, 0),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == str(new_img)
        assert rows[0][1] == 200
        assert rows[0][2] == 13

    def test_clear_expired_handles_already_deleted_image_file(
        self, cache, sample_pdf, tmp_path
    ):
        """L483-484: clear_expired() handles FileNotFoundError when image file
        referenced in DB is already deleted from disk."""
        import sqlite3

        # Save metadata so there's an entry to expire
        cache.save_metadata(sample_pdf, 1, {}, [])

        # Save page images with a non-existent file path
        nonexistent = str(tmp_path / "expired_gone.png")
        cache.save_page_images(
            sample_pdf,
            0,
            [
                {
                    "index": 0,
                    "width": 50,
                    "height": 50,
                    "format": "rgb",
                    "path": nonexistent,
                    "size_bytes": 100,
                }
            ],
        )

        # Backdate accessed_at to force expiration
        with sqlite3.connect(cache.db_path) as conn:
            conn.execute("UPDATE pdf_metadata SET accessed_at = '2020-01-01T00:00:00'")

        # clear_expired tries to unlink nonexistent — should not raise
        cleared = cache.clear_expired()
        assert cleared == 1

        # Verify entries are cleaned up
        with sqlite3.connect(cache.db_path) as conn:
            meta_count = conn.execute("SELECT COUNT(*) FROM pdf_metadata").fetchone()[0]
            img_count = conn.execute("SELECT COUNT(*) FROM page_images").fetchone()[0]
        assert meta_count == 0
        assert img_count == 0

    def test_get_stats_with_missing_images_dir(self, cache):
        """L544-545: get_stats() handles FileNotFoundError from images_dir."""
        from unittest.mock import MagicMock

        # Replace images_dir with a mock that raises on glob
        # (simulates race condition: dir deleted between check and glob)
        mock_dir = MagicMock()
        mock_dir.glob.side_effect = FileNotFoundError("gone")
        cache.images_dir = mock_dir

        stats = cache.get_stats()

        expected_db_size = os.path.getsize(cache.db_path)
        assert stats["cache_size_bytes"] == expected_db_size


class TestGetColumns:
    """Tests for _get_columns helper."""

    def test_returns_column_names_for_existing_table(self, tmp_path):
        import sqlite3

        db = tmp_path / "test.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE foo (id INTEGER, name TEXT)")
        with sqlite3.connect(db) as conn:
            assert _get_columns(conn, "foo") == {"id", "name"}

    def test_returns_empty_set_for_missing_table(self, tmp_path):
        import sqlite3

        db = tmp_path / "test.db"
        with sqlite3.connect(db) as conn:
            assert _get_columns(conn, "nonexistent") == set()


class TestSchemaMigration:
    """Tests for automatic schema migration in _init_db."""

    def test_stale_page_tables_schema_is_migrated(self, tmp_path):
        """page_tables missing 'data' column is dropped and recreated."""
        import sqlite3

        db_path = tmp_path / "cache.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE page_tables (
                    file_path TEXT NOT NULL,
                    page_num  INTEGER NOT NULL,
                    PRIMARY KEY (file_path, page_num)
                )
            """)

        PDFCache(cache_dir=tmp_path)

        with sqlite3.connect(db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(page_tables)")}
        assert "data" in cols

    def test_stale_pdf_metadata_schema_is_migrated(self, tmp_path):
        """pdf_metadata missing 'page_count' column is dropped and recreated."""
        import sqlite3

        db_path = tmp_path / "cache.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE pdf_metadata (
                    file_path TEXT PRIMARY KEY
                )
            """)

        PDFCache(cache_dir=tmp_path)

        with sqlite3.connect(db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(pdf_metadata)")}
        assert {"file_path", "page_count", "file_mtime"}.issubset(cols)

    def test_stale_page_text_schema_is_migrated(self, tmp_path):
        """page_text missing 'text' column is dropped and recreated."""
        import sqlite3

        db_path = tmp_path / "cache.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE page_text (
                    file_path TEXT NOT NULL,
                    page_num  INTEGER NOT NULL,
                    PRIMARY KEY (file_path, page_num)
                )
            """)

        PDFCache(cache_dir=tmp_path)

        with sqlite3.connect(db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(page_text)")}
        assert {"file_path", "page_num", "text"}.issubset(cols)

    def test_valid_page_embeddings_not_dropped_during_migration(self, tmp_path):
        """page_embeddings with correct schema survives migration of stale tables."""
        import sqlite3

        db_path = tmp_path / "cache.db"
        with sqlite3.connect(db_path) as conn:
            # Stale page_tables (will be migrated)
            conn.execute("""
                CREATE TABLE page_tables (
                    file_path TEXT NOT NULL,
                    page_num  INTEGER NOT NULL,
                    PRIMARY KEY (file_path, page_num)
                )
            """)
            # Valid page_embeddings with a row
            conn.execute("""
                CREATE TABLE page_embeddings (
                    file_path  TEXT    NOT NULL,
                    page_num   INTEGER NOT NULL,
                    file_mtime REAL    NOT NULL,
                    embedding  BLOB    NOT NULL,
                    PRIMARY KEY (file_path, page_num)
                )
            """)
            conn.execute(
                "INSERT INTO page_embeddings VALUES (?, ?, ?, ?)",
                ("/fake.pdf", 0, 1234567890.0, b"\x00" * 1536),
            )

        PDFCache(cache_dir=tmp_path)

        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM page_embeddings").fetchone()[0]
        assert count == 1, "page_embeddings rows must survive migration"

    def test_broken_page_embeddings_schema_is_dropped_and_recreated(self, tmp_path):
        """page_embeddings missing 'embedding' column is dropped and recreated."""
        import sqlite3

        db_path = tmp_path / "cache.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE page_embeddings (
                    file_path TEXT NOT NULL,
                    page_num  INTEGER NOT NULL,
                    PRIMARY KEY (file_path, page_num)
                )
            """)

        PDFCache(cache_dir=tmp_path)

        with sqlite3.connect(db_path) as conn:
            cols = {
                row[1] for row in conn.execute("PRAGMA table_info(page_embeddings)")
            }
        assert "embedding" in cols


class TestPageEmbeddingsLifecycle:
    """Embedding rows are removed during invalidation, clearing, and expiry."""

    def test_invalidate_file_removes_embeddings(self, temp_cache_dir, sample_pdf):
        """_invalidate_file() deletes all embeddings for the given file."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        cache.save_page_embeddings(
            sample_pdf, {0: b"\x00" * 1536}, "BAAI/bge-small-en-v1.5"
        )

        cache._invalidate_file(sample_pdf)

        assert (
            cache.get_page_embeddings(sample_pdf, [0], "BAAI/bge-small-en-v1.5") == {}
        )

    def test_clear_all_removes_embeddings(self, temp_cache_dir, sample_pdf):
        """clear_all() removes all embedding rows."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        cache.save_metadata(sample_pdf, 5, {}, [])
        cache.save_page_embeddings(
            sample_pdf, {0: b"\x00" * 1536}, "BAAI/bge-small-en-v1.5"
        )

        cache.clear_all()

        assert cache.get_stats()["embedding_pages"] == 0

    def test_stats_embedding_pages_zero_initially(self, temp_cache_dir):
        """get_stats() returns embedding_pages=0 on a fresh cache."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        assert cache.get_stats()["embedding_pages"] == 0

    def test_stats_embedding_pages_counts_rows(self, temp_cache_dir, sample_pdf):
        """get_stats() counts all cached embedding rows."""
        cache = PDFCache(cache_dir=temp_cache_dir)
        cache.save_page_embeddings(
            sample_pdf, {0: b"\x00" * 1536, 1: b"\x01" * 1536}, "BAAI/bge-small-en-v1.5"
        )
        assert cache.get_stats()["embedding_pages"] == 2

    def test_clear_expired_removes_stale_embeddings(self, temp_cache_dir, sample_pdf):
        """clear_expired() removes embedding rows for expired files."""
        cache = PDFCache(cache_dir=temp_cache_dir, ttl_hours=0)
        cache.save_metadata(sample_pdf, 5, {}, [])
        cache.save_page_embeddings(
            sample_pdf, {0: b"\x00" * 1536}, "BAAI/bge-small-en-v1.5"
        )

        cleared = cache.clear_expired()

        assert cleared >= 1
        assert cache.get_stats()["embedding_pages"] == 0


class TestSectionFTSSchema:
    def test_section_fts_table_created_on_init(self, tmp_path):
        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            import pytest

            pytest.skip("FTS5 not available in this SQLite build")
        with sqlite3.connect(cache.db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "pdf_section_fts" in tables

    def test_section_fts_columns(self, tmp_path):
        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            import pytest

            pytest.skip("FTS5 not available")
        with sqlite3.connect(cache.db_path) as conn:
            cols = {
                row[1] for row in conn.execute("PRAGMA table_info(pdf_section_fts)")
            }
        # FTS5 reports both indexed and UNINDEXED columns via PRAGMA
        assert {
            "file_path",
            "section_id",
            "title",
            "text",
            "start_page",
            "end_page",
        }.issubset(cols)


class TestIndexSections:
    def test_index_then_count(self, tmp_path):
        from pdf_mcp.section_detector import Section

        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            import pytest

            pytest.skip("FTS5 not available")
        sections = [
            Section("Intro", 1, 5, "introduction body text"),
            Section("Methods", 6, 10, "methods body text"),
        ]
        cache.index_sections("/fake/path.pdf", sections)
        with sqlite3.connect(cache.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM pdf_section_fts WHERE file_path = ?",
                ("/fake/path.pdf",),
            ).fetchone()[0]
        assert n == 2

    def test_reindex_replaces_old_entries(self, tmp_path):
        from pdf_mcp.section_detector import Section

        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            import pytest

            pytest.skip("FTS5 not available")
        cache.index_sections("/fake/path.pdf", [Section("A", 1, 1, "a")])
        cache.index_sections(
            "/fake/path.pdf",
            [Section("B", 1, 1, "b"), Section("C", 2, 2, "c")],
        )
        with sqlite3.connect(cache.db_path) as conn:
            titles = sorted(
                row[0]
                for row in conn.execute(
                    "SELECT title FROM pdf_section_fts WHERE file_path = ?",
                    ("/fake/path.pdf",),
                )
            )
        assert titles == ["B", "C"]

    def test_index_isolated_per_file(self, tmp_path):
        from pdf_mcp.section_detector import Section

        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            import pytest

            pytest.skip("FTS5 not available")
        cache.index_sections("/a.pdf", [Section("A1", 1, 1, "a1")])
        cache.index_sections("/b.pdf", [Section("B1", 1, 1, "b1")])
        # Reindexing /a.pdf must not affect /b.pdf
        cache.index_sections("/a.pdf", [Section("A2", 1, 1, "a2")])
        with sqlite3.connect(cache.db_path) as conn:
            a_titles = sorted(
                row[0]
                for row in conn.execute(
                    "SELECT title FROM pdf_section_fts WHERE file_path = ?",
                    ("/a.pdf",),
                )
            )
            b_titles = sorted(
                row[0]
                for row in conn.execute(
                    "SELECT title FROM pdf_section_fts WHERE file_path = ?",
                    ("/b.pdf",),
                )
            )
        assert a_titles == ["A2"]
        assert b_titles == ["B1"]

    def test_no_op_when_fts_unavailable(self, tmp_path, monkeypatch):
        from pdf_mcp.section_detector import Section

        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        # Force fts_available=False to simulate FTS5-less build
        cache.fts_available = False
        # Should not raise; should be a no-op
        cache.index_sections("/p.pdf", [Section("X", 1, 1, "x")])


class TestSearchSectionFTS:
    def test_returns_ranked_sections_by_bm25(self, tmp_path):
        from pdf_mcp.section_detector import Section

        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            pytest.skip("FTS5 not available")
        sections = [
            Section("Intro", 1, 5, "introduction body about graphs"),
            Section("Methods", 6, 10, "graph attention mechanism details"),
            Section("Results", 11, 15, "we observed the following"),
        ]
        cache.index_sections("/fake/p.pdf", sections)
        out = cache.search_section_fts("/fake/p.pdf", "graph attention", max_results=3)
        assert len(out) >= 1
        # Methods has the strongest match for "graph attention"
        assert out[0]["title"] == "Methods"
        # Returned shape includes the expected keys
        assert {"section_id", "title", "start_page", "end_page", "score"}.issubset(
            out[0].keys()
        )

    def test_score_ordering_descending(self, tmp_path):
        from pdf_mcp.section_detector import Section

        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            pytest.skip("FTS5 not available")
        sections = [
            Section("A", 1, 1, "alpha alpha alpha alpha"),
            Section("B", 2, 2, "alpha"),
            Section("C", 3, 3, "alpha alpha"),
        ]
        cache.index_sections("/p.pdf", sections)
        out = cache.search_section_fts("/p.pdf", "alpha", max_results=3)
        # All three should match; scores must be descending
        scores = [r["score"] for r in out]
        assert scores == sorted(scores, reverse=True)

    def test_no_matches_returns_empty(self, tmp_path):
        from pdf_mcp.section_detector import Section

        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            pytest.skip("FTS5 not available")
        cache.index_sections("/p.pdf", [Section("X", 1, 1, "alpha")])
        assert cache.search_section_fts("/p.pdf", "zeta", max_results=3) == []

    def test_max_results_truncates(self, tmp_path):
        from pdf_mcp.section_detector import Section

        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            pytest.skip("FTS5 not available")
        sections = [Section(f"S{i}", i, i, f"alpha {i}") for i in range(1, 6)]
        cache.index_sections("/p.pdf", sections)
        out = cache.search_section_fts("/p.pdf", "alpha", max_results=2)
        assert len(out) == 2

    def test_isolated_per_file(self, tmp_path):
        from pdf_mcp.section_detector import Section

        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            pytest.skip("FTS5 not available")
        cache.index_sections("/a.pdf", [Section("A", 1, 1, "alpha")])
        cache.index_sections("/b.pdf", [Section("B", 1, 1, "alpha")])
        out = cache.search_section_fts("/a.pdf", "alpha", max_results=10)
        titles = [r["title"] for r in out]
        assert titles == ["A"]

    def test_fts_unavailable_returns_empty(self, tmp_path):
        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        cache.fts_available = False
        assert cache.search_section_fts("/p.pdf", "anything", 3) == []


class TestGetSectionFTSCoverage:
    def test_returns_zero_when_unindexed(self, tmp_path):
        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            pytest.skip("FTS5 not available")
        assert cache.get_section_fts_coverage("/never-indexed.pdf") == 0

    def test_returns_section_count_after_indexing(self, tmp_path):
        from pdf_mcp.section_detector import Section

        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            pytest.skip("FTS5 not available")
        cache.index_sections(
            "/p.pdf",
            [
                Section("A", 1, 1, "a"),
                Section("B", 2, 2, "b"),
                Section("C", 3, 3, "c"),
            ],
        )
        assert cache.get_section_fts_coverage("/p.pdf") == 3

    def test_count_drops_to_zero_after_empty_reindex(self, tmp_path):
        from pdf_mcp.section_detector import Section

        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        if not cache.fts_available:
            pytest.skip("FTS5 not available")
        cache.index_sections("/p.pdf", [Section("A", 1, 1, "a")])
        cache.index_sections("/p.pdf", [])  # explicit empty
        assert cache.get_section_fts_coverage("/p.pdf") == 0

    def test_returns_zero_when_fts_unavailable(self, tmp_path):
        cache = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        cache.fts_available = False
        assert cache.get_section_fts_coverage("/p.pdf") == 0


def test_section_embeddings_table_exists(tmp_path):
    from pdf_mcp.cache import PDFCache
    import sqlite3

    cache = PDFCache(cache_dir=tmp_path)
    with sqlite3.connect(cache.db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(section_embeddings)")}
    assert cols == {
        "file_path",
        "section_id",
        "section_key",
        "file_mtime",
        "embedding",
        "model",
        "created_at",
    }


def test_save_and_get_section_embeddings(tmp_path):
    from pdf_mcp.cache import PDFCache

    cache = PDFCache(cache_dir=tmp_path)
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    blobs = {0: b"\x00" * 1536, 1: b"\x01" * 1536}
    keys = {0: "S000:p1:Intro", 1: "S001:p3:Methods"}
    cache.save_section_embeddings(str(pdf_path), blobs, keys, model="bge-small-en-v1.5")

    got = cache.get_section_embeddings(str(pdf_path), [0, 1])
    assert got == blobs


def test_get_section_embeddings_empty_input_returns_empty(tmp_path):
    from pdf_mcp.cache import PDFCache

    cache = PDFCache(cache_dir=tmp_path)
    assert cache.get_section_embeddings("/no/such/path.pdf", []) == {}


def test_section_embeddings_invalidated_on_mtime_change(tmp_path):
    from pdf_mcp.cache import PDFCache
    import os
    import time

    cache = PDFCache(cache_dir=tmp_path)
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    cache.save_section_embeddings(
        str(pdf_path), {0: b"\x00" * 1536}, {0: "S000:p1:T"}, model="m"
    )
    assert cache.get_section_embeddings(str(pdf_path), [0]) == {0: b"\x00" * 1536}

    time.sleep(0.05)
    pdf_path.write_bytes(b"%PDF-1.4\n%modified\n%%EOF\n")
    os.utime(pdf_path, None)

    # Stale rows must be filtered out by _is_cache_valid.
    assert cache.get_section_embeddings(str(pdf_path), [0]) == {}


def test_invalidate_file_drops_section_embeddings(tmp_path):
    from pdf_mcp.cache import PDFCache
    import sqlite3

    cache = PDFCache(cache_dir=tmp_path)
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    cache.save_section_embeddings(
        str(pdf_path), {0: b"\x00" * 1536}, {0: "S000:p1:T"}, model="m"
    )
    cache._invalidate_file(str(pdf_path))

    with sqlite3.connect(cache.db_path) as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM section_embeddings WHERE file_path = ?",
            (str(pdf_path),),
        ).fetchone()
    assert n == 0


def test_clear_all_drops_section_embeddings(tmp_path):
    from pdf_mcp.cache import PDFCache
    import sqlite3

    cache = PDFCache(cache_dir=tmp_path)
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    cache.save_section_embeddings(
        str(pdf_path), {0: b"\x00" * 1536}, {0: "S000:p1:T"}, model="m"
    )
    cache.clear_all()

    with sqlite3.connect(cache.db_path) as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM section_embeddings").fetchone()
    assert n == 0


def test_get_section_embeddings_coverage(tmp_path):
    from pdf_mcp.cache import PDFCache

    cache = PDFCache(cache_dir=tmp_path)
    pdf_path = tmp_path / "x.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    assert cache.get_section_embeddings_coverage(str(pdf_path)) == 0
    cache.save_section_embeddings(
        str(pdf_path),
        {0: b"\x00" * 1536, 2: b"\x02" * 1536},
        {0: "S000:p1:A", 2: "S002:p5:B"},
        model="m",
    )
    assert cache.get_section_embeddings_coverage(str(pdf_path)) == 2
