"""
URL fetching utilities for downloading PDFs from HTTP/HTTPS sources.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import PDFConfig
from urllib.parse import urlparse

import httpx

# Maximum download size: 100 MB
MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024

# Content-Types that are immediately disqualified before any bytes are
# buffered. PDFs may arrive as application/pdf, application/x-pdf, or
# application/octet-stream; anything in this deny list cannot be a PDF.
_DENIED_CONTENT_TYPE_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "image/",
    "audio/",
    "video/",
    "multipart/",
)

# Maximum number of HTTP redirects to follow
MAX_REDIRECTS = 10

_BLOCKED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),  # IPv4 loopback
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),  # reserved
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("::/128"),  # IPv6 unspecified
    ipaddress.ip_network("::ffff:0:0/96"),  # IPv4-mapped IPv6
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64 well-known
    ipaddress.ip_network("100::/64"),  # IPv6 discard prefix
    ipaddress.ip_network("2001:db8::/32"),  # IPv6 documentation
    ipaddress.ip_network("fc00::/7"),  # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IMDS via IPv6
)


def _pick_pinned_ip(hostname: str) -> tuple[str, socket.AddressFamily]:
    """
    Resolve hostname once and pick the first non-blocked address.
    Returns (ip_literal, family). Raises ValueError if the resolution
    yields no addresses or only blocked addresses.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as e:
        raise ValueError(f"DNS resolution failed for {hostname}: {e}") from e
    for info in infos:
        ip_str = str(info[4][0])
        ip = ipaddress.ip_address(ip_str)
        if any(ip in net for net in _BLOCKED_NETWORKS if ip.version == net.version):
            continue
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None and any(
            mapped in net for net in _BLOCKED_NETWORKS if net.version == 4
        ):
            continue
        return ip_str, info[0]  # family is socket.AF_INET / AF_INET6
    raise ValueError(
        f"URL host resolves to a blocked IP on the SSRF deny list "
        f"(loopback / RFC 1918 / link-local / IMDS / IPv6 ULA): {hostname}"
    )


class URLFetcher:
    """
    Fetches PDFs from URLs and caches them locally.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        timeout: int = 60,
        config: PDFConfig | None = None,
    ):
        """
        Initialize URL fetcher.

        Args:
            cache_dir: Directory to store downloaded PDFs. Defaults to the
                per-user cache root (~/.cache/pdf-mcp/downloads), matching the
                main SQLite cache so all artifacts share one configurable root
                and two local users never collide on a fixed /tmp path.
            timeout: HTTP timeout in seconds
        """
        if cache_dir is None:
            cache_dir = Path.home() / ".cache" / "pdf-mcp" / "downloads"

        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Restrict permissions so other local users can't read downloads.
        # Fail-soft: on a shared host the dir may already exist owned by a
        # different user, where chmod raises PermissionError. A best-effort
        # tightening must not crash startup (issue #15).
        try:
            os.chmod(self.cache_dir, 0o700)
        except OSError:
            pass
        self.timeout = timeout
        self._config = config
        self._url_to_path: dict[str, Path] = {}

    @staticmethod
    def _is_blocked_ip(hostname: str) -> bool:
        """Check if hostname resolves to any blocked IP range.

        Also unwraps IPv4-mapped IPv6 addresses (`::ffff:1.2.3.4`) and
        re-tests the IPv4 form against the IPv4 blocked networks, so
        `::ffff:127.0.0.1` is rejected even if the `::ffff:0:0/96`
        network were ever removed.
        """
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
            for addr_info in addr_infos:
                ip_str = addr_info[4][0]
                ip = ipaddress.ip_address(ip_str)
                if any(
                    ip in net for net in _BLOCKED_NETWORKS if ip.version == net.version
                ):
                    return True
                mapped = getattr(ip, "ipv4_mapped", None)
                if mapped is not None:
                    if any(
                        mapped in net for net in _BLOCKED_NETWORKS if net.version == 4
                    ):
                        return True
        except (OSError, ValueError):
            return True
        return False

    def _validate_url(self, url: str) -> None:
        """
        Standalone URL validation (HTTPS scheme, IP block list, config allow/deny).
        The fetch path uses _validate_url_no_dns + _pick_pinned_ip instead to
        perform exactly one DNS lookup per hop and pin the resolved IP.

        Blocks:
        - Non-HTTPS schemes (http, ftp, file, data, etc.)
        - IPs in blocked ranges: loopback, RFC 1918, link-local, reserved, IPv6

        Raises:
            ValueError: If URL targets a blocked address or uses a blocked scheme
        """
        parsed = urlparse(url)

        if parsed.scheme != "https":
            raise ValueError(
                f"Only HTTPS URLs are supported. "
                f"Update your URL to use https:// (got: {parsed.scheme})"
            )

        hostname = parsed.hostname
        if not hostname:
            raise ValueError(f"Could not extract hostname from URL: {url}")

        if self._is_blocked_ip(hostname):
            raise ValueError(
                f"URL host resolves to a blocked IP on the SSRF deny list "
                f"(loopback / RFC 1918 / link-local / IMDS / IPv6 ULA): {url}"
            )

        if self._config is not None:
            self._config.check_url_host(hostname)

    def _validate_url_no_dns(self, url: str) -> None:
        """
        Validate URL scheme and config rules without performing DNS resolution.
        IP-range blocking is handled separately by _pick_pinned_ip.

        Used inside the fetch loop so that each hop makes exactly one
        getaddrinfo call (via _pick_pinned_ip), closing the DNS-rebinding
        TOCTOU gap.

        Raises:
            ValueError: If URL uses a blocked scheme or is disallowed by config
        """
        parsed = urlparse(url)

        if parsed.scheme != "https":
            raise ValueError(
                f"Only HTTPS URLs are supported. "
                f"Update your URL to use https:// (got: {parsed.scheme})"
            )

        hostname = parsed.hostname
        if not hostname:
            raise ValueError(f"Could not extract hostname from URL: {url}")

        if self._config is not None:
            self._config.check_url_host(hostname)

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
        """Check if source looks like an HTTP(S) URL.

        Returns True for both http:// and https:// so that http URLs are
        routed through the fetch validator (which rejects them with a
        clear "HTTPS only" message) rather than silently falling through
        to the local-path branch and producing a misleading
        "PDF file not found" error.
        """
        return source.startswith("https://") or source.startswith("http://")

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
        # Validate URL scheme and config rules. IP-range blocking is deferred
        # to _pick_pinned_ip inside the loop so there is exactly one
        # getaddrinfo call per hop, closing the DNS-rebinding TOCTOU gap.
        self._validate_url_no_dns(url)

        # Check cache first
        if not force_refresh:
            cached_path = self.get_local_path(url)
            if cached_path:
                return cached_path

        # Download with manual redirect handling to validate each hop
        # before connecting (prevents TOCTOU SSRF via redirects).
        # IP pinning: resolve the hostname once per hop and rewrite the
        # URL to the pinned IP so httpx connects to the address we
        # validated — closing the DNS-rebinding TOCTOU gap.
        current_url = url
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS):
                # Validate scheme + config; IP check is done by _pick_pinned_ip
                # below to keep exactly one getaddrinfo call per hop (TOCTOU fix).
                self._validate_url_no_dns(current_url)

                parsed = urlparse(current_url)
                hostname = parsed.hostname or ""
                pinned_ip, af = _pick_pinned_ip(hostname)
                if af == socket.AF_INET6:
                    ip_host = f"[{pinned_ip}]"
                else:
                    ip_host = pinned_ip
                rebuilt_netloc = f"{ip_host}:{parsed.port}" if parsed.port else ip_host
                rebuilt = parsed._replace(netloc=rebuilt_netloc).geturl()

                request_headers = {"Host": parsed.netloc}
                request_extensions: dict[str, Any] = {"sni_hostname": hostname}

                with client.stream(
                    "GET",
                    rebuilt,
                    headers=request_headers,
                    extensions=request_extensions,
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("Redirect with no target URL")
                        # Resolve the Location against the hostname-based
                        # current_url, NOT response.next_request.url. We rewrote
                        # this hop's request to the pinned IP, so httpx resolves
                        # a *relative* Location against that IP URL and drops the
                        # hostname — the next hop would then verify the TLS cert
                        # against the IP literal and fail (issue #16). Joining
                        # against current_url preserves the hostname; the next
                        # loop iteration re-validates and re-pins it, so the
                        # SSRF / DNS-rebinding protection is fully intact.
                        current_url = str(httpx.URL(current_url).join(location))
                        continue

                    response.raise_for_status()

                    early_ct = response.headers.get("content-type", "").lower()
                    if any(
                        early_ct.startswith(p) for p in _DENIED_CONTENT_TYPE_PREFIXES
                    ):
                        raise ValueError(
                            f"URL content-type {early_ct!r} is not a PDF: "
                            f"{current_url}"
                        )

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
