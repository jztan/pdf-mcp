"""
SQLite-based cache for PDF data persistence across MCP server restarts.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class PDFCache:
    """
    SQLite-based cache for PDF metadata and page text.
    
    Persists data to disk so it survives MCP server process restarts.
    Uses file modification time for cache invalidation.
    """
    
    def __init__(self, cache_dir: Path | None = None, ttl_hours: int = 24):
        """
        Initialize the cache.
        
        Args:
            cache_dir: Directory to store cache database. Defaults to ~/.cache/pdf-mcp
            ttl_hours: Time-to-live for cache entries in hours
        """
        if cache_dir is None:
            cache_dir = Path.home() / ".cache" / "pdf-mcp"
        
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / "cache.db"
        self.ttl_hours = ttl_hours
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
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
                
                -- Page images cache (stores base64)
                CREATE TABLE IF NOT EXISTS page_images (
                    file_path TEXT NOT NULL,
                    page_num INTEGER NOT NULL,
                    image_index INTEGER NOT NULL,
                    file_mtime REAL NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    format TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (file_path, page_num, image_index)
                );
                
                -- Indexes for faster lookups
                CREATE INDEX IF NOT EXISTS idx_page_text_path ON page_text(file_path);
                CREATE INDEX IF NOT EXISTS idx_page_images_path ON page_images(file_path);
                CREATE INDEX IF NOT EXISTS idx_metadata_accessed ON pdf_metadata(accessed_at);
            """)
    
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
                """SELECT file_mtime, file_size, page_count, metadata, toc 
                   FROM pdf_metadata WHERE file_path = ?""",
                (path,)
            ).fetchone()
            
            if row is None:
                return None
            
            # Validate cache
            if not self._is_cache_valid(path, row["file_mtime"]):
                self._invalidate_file(path)
                return None
            
            # Update access time
            conn.execute(
                "UPDATE pdf_metadata SET accessed_at = CURRENT_TIMESTAMP WHERE file_path = ?",
                (path,)
            )
            
            return {
                "file_path": path,
                "file_size": row["file_size"],
                "page_count": row["page_count"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
                "toc": json.loads(row["toc"]) if row["toc"] else [],
            }
    
    def save_metadata(
        self,
        path: str,
        page_count: int,
        metadata: dict[str, Any],
        toc: list[Any]
    ) -> None:
        """
        Save PDF metadata to cache.
        
        Args:
            path: Path to PDF file
            page_count: Total number of pages
            metadata: PDF metadata dict
            toc: Table of contents list
        """
        mtime, size = self._get_file_info(path)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO pdf_metadata 
                   (file_path, file_mtime, file_size, page_count, metadata, toc, accessed_at)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (path, mtime, size, page_count, json.dumps(metadata), json.dumps(toc))
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
                (path, page_num)
            ).fetchone()
            
            if row is None:
                return None
            
            if not self._is_cache_valid(path, row[1]):
                return None
            
            return row[0]
    
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
                f"""SELECT page_num, text, file_mtime FROM page_text 
                    WHERE file_path = ? AND page_num IN ({placeholders})""",
                (path, *page_nums)
            ).fetchall()
            
            result = {}
            for page_num, text, mtime in rows:
                if self._is_cache_valid(path, mtime):
                    result[page_num] = text
            
            return result
    
    def save_page_text(self, path: str, page_num: int, text: str) -> None:
        """
        Save page text to cache.
        
        Args:
            path: Path to PDF file
            page_num: Page number (0-indexed)
            text: Extracted text content
        """
        mtime, _ = self._get_file_info(path)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO page_text 
                   (file_path, page_num, file_mtime, text, text_length)
                   VALUES (?, ?, ?, ?, ?)""",
                (path, page_num, mtime, text, len(text))
            )
    
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
                   (file_path, page_num, file_mtime, text, text_length)
                   VALUES (?, ?, ?, ?, ?)""",
                [(path, page_num, mtime, text, len(text)) for page_num, text in pages.items()]
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
                """SELECT image_index, width, height, format, data, file_mtime 
                   FROM page_images 
                   WHERE file_path = ? AND page_num = ?
                   ORDER BY image_index""",
                (path, page_num)
            ).fetchall()
            
            if not rows:
                return None
            
            # Check if any row is invalid
            if not all(self._is_cache_valid(path, row["file_mtime"]) for row in rows):
                return None
            
            return [
                {
                    "page": page_num + 1,
                    "index": row["image_index"],
                    "width": row["width"],
                    "height": row["height"],
                    "format": row["format"],
                    "data": row["data"],
                }
                for row in rows
            ]
    
    def save_page_images(self, path: str, page_num: int, images: list[dict[str, Any]]) -> None:
        """
        Save page images to cache.
        
        Args:
            path: Path to PDF file
            page_num: Page number (0-indexed)
            images: List of image dicts with width, height, format, data
        """
        mtime, _ = self._get_file_info(path)
        
        with sqlite3.connect(self.db_path) as conn:
            # Clear existing images for this page
            conn.execute(
                "DELETE FROM page_images WHERE file_path = ? AND page_num = ?",
                (path, page_num)
            )
            
            # Insert new images
            conn.executemany(
                """INSERT INTO page_images 
                   (file_path, page_num, image_index, file_mtime, width, height, format, data)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (path, page_num, img.get("index", i), mtime, 
                     img["width"], img["height"], img["format"], img["data"])
                    for i, img in enumerate(images)
                ]
            )
    
    # ==================== Cache Management ====================
    
    def _invalidate_file(self, path: str) -> None:
        """Remove all cache entries for a file."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM pdf_metadata WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM page_text WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM page_images WHERE file_path = ?", (path,))
    
    def clear_expired(self) -> int:
        """
        Remove expired cache entries.
        
        Returns:
            Number of files cleared
        """
        cutoff = datetime.now() - timedelta(hours=self.ttl_hours)
        
        with sqlite3.connect(self.db_path) as conn:
            # Get expired file paths
            expired = conn.execute(
                "SELECT file_path FROM pdf_metadata WHERE accessed_at < ?",
                (cutoff,)
            ).fetchall()
            
            expired_paths = [row[0] for row in expired]
            
            if expired_paths:
                placeholders = ",".join("?" * len(expired_paths))
                conn.execute(
                    f"DELETE FROM pdf_metadata WHERE file_path IN ({placeholders})",
                    expired_paths
                )
                conn.execute(
                    f"DELETE FROM page_text WHERE file_path IN ({placeholders})",
                    expired_paths
                )
                conn.execute(
                    f"DELETE FROM page_images WHERE file_path IN ({placeholders})",
                    expired_paths
                )
            
            return len(expired_paths)
    
    def clear_all(self) -> None:
        """Clear entire cache."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM pdf_metadata")
            conn.execute("DELETE FROM page_text")
            conn.execute("DELETE FROM page_images")
    
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
            
            # Count images
            stats["total_images"] = conn.execute(
                "SELECT COUNT(*) FROM page_images"
            ).fetchone()[0]
            
            # Total text size
            row = conn.execute(
                "SELECT SUM(text_length) FROM page_text"
            ).fetchone()
            stats["total_text_chars"] = row[0] or 0
            
            # Database file size
            stats["cache_size_bytes"] = os.path.getsize(self.db_path)
            stats["cache_size_mb"] = round(stats["cache_size_bytes"] / (1024 * 1024), 2)
            
            return stats
