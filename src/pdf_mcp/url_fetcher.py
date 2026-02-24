"""
URL fetching utilities for downloading PDFs from HTTP/HTTPS sources.
"""

import hashlib
import ipaddress
import os
import socket
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

# Maximum download size: 100 MB
MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024

# Maximum number of HTTP redirects to follow
MAX_REDIRECTS = 10


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
        # Restrict permissions on cache directory so other users can't read downloads
        os.chmod(self.cache_dir, 0o700)
        self.timeout = timeout
        self._url_to_path: dict[str, Path] = {}

    @staticmethod
    def _is_private_ip(hostname: str) -> bool:
        """Check if a hostname resolves to a private/reserved IP address."""
        try:
            # Resolve hostname to IP addresses
            addr_infos = socket.getaddrinfo(hostname, None)
            for addr_info in addr_infos:
                ip_str = addr_info[4][0]
                ip = ipaddress.ip_address(ip_str)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_multicast
                ):
                    return True
        except (OSError, ValueError):
            # If we can't resolve, treat as potentially dangerous
            return True
        return False

    def _validate_url(self, url: str) -> None:
        """
        Validate URL to prevent SSRF attacks.

        Blocks:
        - Private/internal IP ranges (10.x, 172.16-31.x, 192.168.x, 127.x)
        - Link-local addresses (169.254.x - including cloud metadata endpoints)
        - Localhost
        - Non-HTTP(S) schemes

        Raises:
            ValueError: If URL targets a blocked address
        """
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Only HTTP and HTTPS URLs are allowed, got: {parsed.scheme}"
            )

        hostname = parsed.hostname
        if not hostname:
            raise ValueError(f"Could not extract hostname from URL: {url}")

        # Block obvious localhost references
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            raise ValueError(f"URLs targeting localhost are not allowed: {url}")

        # Resolve hostname and check if it points to private/reserved IPs
        if self._is_private_ip(hostname):
            raise ValueError(
                f"URL resolves to a private/reserved IP address and is blocked: {url}"
            )

    def _get_cache_filename(self, url: str) -> str:
        """Generate cache filename from URL."""
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]

        # Try to extract original filename from URL
        parsed = urlparse(url)
        path = parsed.path

        if path.endswith(".pdf"):
            original_name = os.path.basename(path)
            # Sanitize filename
            safe_name = "".join(c for c in original_name if c.isalnum() or c in "._-")
            return f"{url_hash}_{safe_name}"

        return f"{url_hash}.pdf"

    def is_url(self, source: str) -> bool:
        """Check if source is a URL."""
        return source.startswith(("http://", "https://"))

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
            ValueError: If URL doesn't return a PDF or targets a blocked address
        """
        # Validate URL to prevent SSRF
        self._validate_url(url)

        # Check cache first
        if not force_refresh:
            cached_path = self.get_local_path(url)
            if cached_path:
                return cached_path

        # Download with manual redirect handling to validate each hop
        # before connecting (prevents TOCTOU SSRF via redirects)
        current_url = url
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS):
                with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        next_req = response.next_request
                        if next_req is None:
                            raise ValueError("Redirect with no target URL")
                        redirect_url = str(next_req.url)
                        self._validate_url(redirect_url)
                        current_url = redirect_url
                        continue

                    response.raise_for_status()

                    # Check Content-Length header if available
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > MAX_DOWNLOAD_SIZE:
                        raise ValueError(
                            f"PDF file too large: {int(content_length)} bytes "
                            f"(max {MAX_DOWNLOAD_SIZE} bytes)"
                        )

                    # Read response with size limit
                    chunks: list[bytes] = []
                    total_size = 0
                    for chunk in response.iter_bytes(chunk_size=8192):
                        total_size += len(chunk)
                        if total_size > MAX_DOWNLOAD_SIZE:
                            raise ValueError(
                                f"PDF download exceeded maximum size of "
                                f"{MAX_DOWNLOAD_SIZE} bytes"
                            )
                        chunks.append(chunk)

                    content = b"".join(chunks)

                    # Verify content type
                    content_type = response.headers.get("content-type", "")
                    if "pdf" not in content_type.lower():
                        # Check magic bytes when Content-Type is not PDF
                        if not content.startswith(b"%PDF"):
                            raise ValueError(f"URL does not appear to be a PDF: {url}")
                    break
            else:
                raise ValueError(f"Too many redirects (max {MAX_REDIRECTS})")

        # Save to cache with restricted permissions
        filename = self._get_cache_filename(url)
        local_path = self.cache_dir / filename

        fd = os.open(str(local_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)

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
