# tests/test_url_fetcher.py
"""Tests for pdf_mcp.url_fetcher module."""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from pdf_mcp.url_fetcher import URLFetcher


@pytest.fixture
def url_fetcher(temp_cache_dir):
    """Create URLFetcher with temp directory."""
    return URLFetcher(cache_dir=temp_cache_dir / "downloads")


@pytest.fixture
def valid_pdf_bytes():
    """Valid PDF content (minimal)."""
    return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


def _mock_stream_response(content, headers=None, is_redirect=False, redirect_url=None):
    """Create a mock streaming response for httpx.Client.stream()."""
    if headers is None:
        headers = {}
    mock_response = MagicMock()
    mock_response.headers = headers
    mock_response.url = "https://example.com/test.pdf"
    mock_response.is_redirect = is_redirect
    mock_response.raise_for_status = Mock()
    mock_response.iter_bytes = Mock(return_value=iter([content]))
    mock_response.__enter__ = Mock(return_value=mock_response)
    mock_response.__exit__ = Mock(return_value=False)
    if is_redirect and redirect_url:
        mock_next_request = MagicMock()
        mock_next_request.url = redirect_url
        mock_response.next_request = mock_next_request
    return mock_response


class TestFetch:
    """Tests for URLFetcher.fetch() method."""

    @patch.object(URLFetcher, '_validate_url')
    def test_successful_download(self, mock_validate, url_fetcher, valid_pdf_bytes):
        """Successful download saves file and returns path."""
        url = "https://example.com/test.pdf"

        mock_response = _mock_stream_response(valid_pdf_bytes, {"content-type": "application/pdf"})

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.stream.return_value = mock_response

            result = url_fetcher.fetch(url)

        assert result.exists()
        assert result.read_bytes() == valid_pdf_bytes
        mock_validate.assert_called_once_with(url)

    @patch.object(URLFetcher, '_validate_url')
    def test_invalid_content_raises_valueerror(self, mock_validate, url_fetcher):
        """Non-PDF content raises ValueError."""
        url = "https://example.com/notapdf.html"

        mock_response = _mock_stream_response(b"<html>Not a PDF</html>", {"content-type": "text/html"})

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.stream.return_value = mock_response

            with pytest.raises(ValueError, match="does not appear to be a PDF"):
                url_fetcher.fetch(url)

    @patch.object(URLFetcher, '_validate_url')
    def test_force_refresh_bypasses_cache(self, mock_validate, url_fetcher, valid_pdf_bytes):
        """force_refresh=True re-downloads even if cached."""
        url = "https://example.com/refresh.pdf"

        mock_response = _mock_stream_response(valid_pdf_bytes, {"content-type": "application/pdf"})

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.stream.return_value = mock_response

            # First fetch
            path1 = url_fetcher.fetch(url)

            # Second fetch with force_refresh - should stream again
            path2 = url_fetcher.fetch(url, force_refresh=True)

            # httpx.Client().stream should be called twice
            assert mock_client.return_value.stream.call_count == 2


class TestGetCacheFilename:
    """Tests for URLFetcher._get_cache_filename() method."""

    def test_pdf_url_extracts_name(self, url_fetcher):
        """PDF URL extracts original filename."""
        url = "https://example.com/path/document.pdf"
        filename = url_fetcher._get_cache_filename(url)

        assert filename.endswith("_document.pdf")
        assert len(filename) > len("document.pdf")  # Has hash prefix

    def test_non_pdf_url_uses_hash(self, url_fetcher):
        """Non-PDF URL uses hash-based filename."""
        url = "https://example.com/api/download?id=123"
        filename = url_fetcher._get_cache_filename(url)

        assert filename.endswith(".pdf")
        assert "_" not in filename

    def test_special_chars_sanitized(self, url_fetcher):
        """Special characters are removed from filename."""
        url = "https://example.com/path/my%20doc!@#$.pdf"
        filename = url_fetcher._get_cache_filename(url)

        # Should only contain alphanumeric, dots, underscores, hyphens
        base = filename.split("_", 1)[-1] if "_" in filename else filename
        assert all(c.isalnum() or c in "._-" for c in base)

    def test_deterministic_hash(self, url_fetcher):
        """Same URL produces same filename."""
        url = "https://example.com/test.pdf"
        filename1 = url_fetcher._get_cache_filename(url)
        filename2 = url_fetcher._get_cache_filename(url)

        assert filename1 == filename2


class TestGetLocalPath:
    """Tests for URLFetcher.get_local_path() method."""

    def test_cache_miss_returns_none(self, url_fetcher):
        """Uncached URL returns None."""
        result = url_fetcher.get_local_path("https://example.com/uncached.pdf")
        assert result is None

    def test_memory_cache_hit(self, url_fetcher, temp_cache_dir):
        """URL in memory cache returns path."""
        url = "https://example.com/cached.pdf"
        cached_path = temp_cache_dir / "downloads" / "test.pdf"
        cached_path.parent.mkdir(parents=True, exist_ok=True)
        cached_path.write_bytes(b"%PDF-1.4")

        url_fetcher._url_to_path[url] = cached_path

        result = url_fetcher.get_local_path(url)
        assert result == cached_path

    def test_stale_memory_cache_returns_none(self, url_fetcher, temp_cache_dir):
        """Memory cache with deleted file returns None."""
        url = "https://example.com/deleted.pdf"
        deleted_path = temp_cache_dir / "downloads" / "deleted.pdf"

        url_fetcher._url_to_path[url] = deleted_path

        result = url_fetcher.get_local_path(url)
        assert result is None

    def test_disk_cache_discovery(self, url_fetcher):
        """File on disk but not in memory is discovered."""
        url = "https://example.com/ondisk.pdf"
        filename = url_fetcher._get_cache_filename(url)
        disk_path = url_fetcher.cache_dir / filename
        disk_path.write_bytes(b"%PDF-1.4")

        # Not in memory cache
        assert url not in url_fetcher._url_to_path

        result = url_fetcher.get_local_path(url)

        assert result == disk_path
        assert url in url_fetcher._url_to_path  # Now in memory


class TestClearCache:
    """Tests for URLFetcher.clear_cache() method."""

    def test_oserror_handling(self, url_fetcher):
        """OSError during deletion is handled gracefully."""
        # Create a file
        test_file = url_fetcher.cache_dir / "test.pdf"
        test_file.write_bytes(b"%PDF")

        with patch.object(Path, "unlink", side_effect=OSError("Permission denied")):
            # Should not raise
            count = url_fetcher.clear_cache()

        # Count may be 0 since unlink failed
        assert isinstance(count, int)


class TestSSRFProtection:
    """Tests for SSRF prevention in URL validation."""

    def test_localhost_blocked(self, url_fetcher):
        """URLs targeting localhost are blocked."""
        for url in [
            "https://localhost/secret.pdf",
            "https://127.0.0.1/secret.pdf",
            "https://0.0.0.0/secret.pdf",
        ]:
            with pytest.raises(ValueError, match="localhost"):
                url_fetcher._validate_url(url)

    def test_private_ip_blocked(self, url_fetcher):
        """URLs targeting private IPs are blocked."""
        with patch.object(URLFetcher, '_is_private_ip', return_value=True):
            with pytest.raises(ValueError, match="private/reserved"):
                url_fetcher._validate_url("https://internal-server.corp/secret.pdf")

    def test_non_http_scheme_blocked(self, url_fetcher):
        """Non-HTTP(S) schemes are blocked."""
        with pytest.raises(ValueError, match="Only HTTP and HTTPS"):
            url_fetcher._validate_url("ftp://example.com/test.pdf")

    def test_public_ip_allowed(self, url_fetcher):
        """Public IPs pass validation."""
        with patch.object(URLFetcher, '_is_private_ip', return_value=False):
            # Should not raise
            url_fetcher._validate_url("https://public-server.com/test.pdf")

    def test_is_private_ip_loopback(self):
        """Loopback addresses are detected as private."""
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, '', ('127.0.0.1', 0)),
        ]):
            assert URLFetcher._is_private_ip("localhost") is True

    def test_is_private_ip_rfc1918(self):
        """RFC 1918 private addresses are detected."""
        for ip in ['10.0.0.1', '172.16.0.1', '192.168.1.1']:
            with patch("socket.getaddrinfo", return_value=[
                (2, 1, 6, '', (ip, 0)),
            ]):
                assert URLFetcher._is_private_ip("some-host") is True

    def test_is_private_ip_link_local(self):
        """Link-local addresses (cloud metadata) are detected."""
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, '', ('169.254.169.254', 0)),
        ]):
            assert URLFetcher._is_private_ip("metadata.google") is True

    def test_is_private_ip_public(self):
        """Public IPs are not flagged as private."""
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, '', ('93.184.216.34', 0)),
        ]):
            assert URLFetcher._is_private_ip("example.com") is False

    def test_dns_failure_treated_as_private(self):
        """DNS resolution failure is treated as potentially dangerous."""
        with patch("socket.getaddrinfo", side_effect=OSError("DNS failed")):
            assert URLFetcher._is_private_ip("unknown-host") is True


class TestDownloadSizeLimit:
    """Tests for download size limits."""

    @patch.object(URLFetcher, '_validate_url')
    def test_content_length_over_limit_rejected(self, mock_validate, url_fetcher):
        """Content-Length header exceeding limit raises ValueError."""
        url = "https://example.com/huge.pdf"

        mock_response = _mock_stream_response(
            b"", {"content-type": "application/pdf", "content-length": "200000000"},
        )
        mock_response.is_redirect = False

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.stream.return_value = mock_response

            with pytest.raises(ValueError, match="too large"):
                url_fetcher.fetch(url)


class TestRedirectSSRFValidation:
    """Tests for SSRF validation on redirects."""

    @patch.object(URLFetcher, '_validate_url')
    def test_redirect_to_private_ip_blocked(self, mock_validate, url_fetcher, valid_pdf_bytes):
        """Redirect to private IP is validated before following."""
        url = "https://public.example.com/paper.pdf"
        redirect_url = "http://169.254.169.254/latest/meta-data/"

        # First call passes (initial URL), second call raises (redirect target)
        mock_validate.side_effect = [None, ValueError("URL resolves to a private/reserved IP")]

        redirect_response = _mock_stream_response(
            b"", is_redirect=True, redirect_url=redirect_url,
        )

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.stream.return_value = redirect_response

            with pytest.raises(ValueError, match="private/reserved"):
                url_fetcher.fetch(url)

    @patch.object(URLFetcher, '_validate_url')
    def test_redirect_to_public_url_allowed(self, mock_validate, url_fetcher, valid_pdf_bytes):
        """Redirect to public URL is allowed and followed."""
        url = "https://example.com/old.pdf"
        redirect_url = "https://cdn.example.com/new.pdf"

        redirect_response = _mock_stream_response(
            b"", is_redirect=True, redirect_url=redirect_url,
        )
        final_response = _mock_stream_response(
            valid_pdf_bytes, {"content-type": "application/pdf"},
        )

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.stream.side_effect = [redirect_response, final_response]

            result = url_fetcher.fetch(url)

        assert result.exists()
        assert result.read_bytes() == valid_pdf_bytes
        # validate_url called for initial URL + redirect target
        assert mock_validate.call_count == 2

    @patch.object(URLFetcher, '_validate_url')
    def test_too_many_redirects_raises(self, mock_validate, url_fetcher):
        """Exceeding max redirects raises ValueError."""
        url = "https://example.com/loop.pdf"

        redirect_response = _mock_stream_response(
            b"", is_redirect=True, redirect_url="https://example.com/loop.pdf",
        )

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.stream.return_value = redirect_response

            with pytest.raises(ValueError, match="Too many redirects"):
                url_fetcher.fetch(url)
