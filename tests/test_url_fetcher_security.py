"""Regression tests for url_fetcher security fixes (v1.13.0)."""

import socket
from unittest.mock import MagicMock, patch

import pytest

from pdf_mcp.url_fetcher import URLFetcher


@pytest.fixture
def fetcher(tmp_path):
    return URLFetcher(cache_dir=tmp_path / "downloads")


def _mock_response(
    *,
    status_code: int = 200,
    headers: dict | None = None,
    body: bytes = b"",
    is_redirect: bool = False,
):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.is_redirect = is_redirect
    resp.next_request = None
    resp.raise_for_status = MagicMock()
    resp.iter_bytes = MagicMock(return_value=iter([body]))
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_rejects_text_html_content_type(fetcher, monkeypatch):
    """Content-Type: text/html must be rejected before any bytes are read."""
    monkeypatch.setattr(
        URLFetcher, "_is_blocked_ip", staticmethod(lambda host: False)
    )
    resp = _mock_response(headers={"content-type": "text/html; charset=utf-8"})
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.stream = MagicMock(return_value=resp)

    with patch("pdf_mcp.url_fetcher.httpx.Client", return_value=fake_client):
        with pytest.raises(ValueError, match="not.*PDF|content[- ]type"):
            fetcher.fetch("https://example.com/x.pdf")

    resp.iter_bytes.assert_not_called()


def _addrinfo_for(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 0, "", (ip, 0))]


def test_blocks_ipv4_mapped_ipv6_loopback(fetcher):
    """::ffff:127.0.0.1 must be rejected as loopback after unwrapping."""
    with patch(
        "pdf_mcp.url_fetcher.socket.getaddrinfo",
        return_value=_addrinfo_for("::ffff:127.0.0.1"),
    ):
        with pytest.raises(ValueError, match="blocked"):
            fetcher.fetch("https://malicious.example/x.pdf")


def test_blocks_aws_imds_ipv6(fetcher):
    """fd00:ec2::254 (AWS IMDS over IPv6) must be rejected."""
    with patch(
        "pdf_mcp.url_fetcher.socket.getaddrinfo",
        return_value=_addrinfo_for("fd00:ec2::254"),
    ):
        with pytest.raises(ValueError, match="blocked"):
            fetcher.fetch("https://malicious.example/x.pdf")
