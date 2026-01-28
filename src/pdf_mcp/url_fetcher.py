"""
URL fetching utilities for downloading PDFs from HTTP/HTTPS sources.
"""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


class URLFetcher:
    """
    Fetches PDFs from URLs and caches them locally.
    """
    
    def __init__(self, cache_dir: Path | None = None, timeout: int = 60):
        """
        Initialize URL fetcher.
        
        Args:
            cache_dir: Directory to store downloaded PDFs. Defaults to temp dir.
            timeout: HTTP timeout in seconds
        """
        if cache_dir is None:
            cache_dir = Path(tempfile.gettempdir()) / "pdf-mcp" / "downloads"
        
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._url_to_path: dict[str, Path] = {}
    
    def _get_cache_filename(self, url: str) -> str:
        """Generate cache filename from URL."""
        url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
        
        # Try to extract original filename from URL
        parsed = urlparse(url)
        path = parsed.path
        
        if path.endswith('.pdf'):
            original_name = os.path.basename(path)
            # Sanitize filename
            safe_name = "".join(c for c in original_name if c.isalnum() or c in '._-')
            return f"{url_hash}_{safe_name}"
        
        return f"{url_hash}.pdf"
    
    def is_url(self, source: str) -> bool:
        """Check if source is a URL."""
        return source.startswith(('http://', 'https://'))
    
    def get_local_path(self, url: str) -> Path | None:
        """
        Get local path for a URL if already downloaded.
        
        Args:
            url: URL to check
            
        Returns:
            Local path if cached, None otherwise
        """
        if url in self._url_to_path:
            path = self._url_to_path[url]
            if path.exists():
                return path
        
        # Check disk cache
        filename = self._get_cache_filename(url)
        path = self.cache_dir / filename
        
        if path.exists():
            self._url_to_path[url] = path
            return path
        
        return None
    
    def fetch(self, url: str, force_refresh: bool = False) -> Path:
        """
        Fetch PDF from URL and return local path.
        
        Args:
            url: URL to fetch
            force_refresh: If True, re-download even if cached
            
        Returns:
            Path to local PDF file
            
        Raises:
            httpx.HTTPError: If download fails
            ValueError: If URL doesn't return a PDF
        """
        # Check cache first
        if not force_refresh:
            cached_path = self.get_local_path(url)
            if cached_path:
                return cached_path
        
        # Download
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            
            # Verify content type
            content_type = response.headers.get('content-type', '')
            if 'pdf' not in content_type.lower() and not url.lower().endswith('.pdf'):
                # Check magic bytes
                if not response.content.startswith(b'%PDF'):
                    raise ValueError(f"URL does not appear to be a PDF: {url}")
        
        # Save to cache
        filename = self._get_cache_filename(url)
        local_path = self.cache_dir / filename
        
        with open(local_path, 'wb') as f:
            f.write(response.content)
        
        self._url_to_path[url] = local_path
        
        return local_path
    
    def clear_cache(self) -> int:
        """
        Clear all downloaded PDFs.
        
        Returns:
            Number of files deleted
        """
        count = 0
        for path in self.cache_dir.glob("*.pdf"):
            try:
                path.unlink()
                count += 1
            except OSError:
                pass
        
        self._url_to_path.clear()
        return count
    
    def get_cache_stats(self) -> dict[str, Any]:
        """Get statistics about URL cache."""
        files = list(self.cache_dir.glob("*.pdf"))
        total_size = sum(f.stat().st_size for f in files)
        
        return {
            "cached_files": len(files),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "cache_dir": str(self.cache_dir),
        }
