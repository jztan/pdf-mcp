# tests/test_cache.py
"""Tests for pdf_mcp.cache module - edge cases."""

import os
import time
import tempfile
from pathlib import Path

import pytest

from pdf_mcp.cache import PDFCache


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
            conn.execute(
                """CREATE TABLE page_images (
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
                )"""
            )

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
