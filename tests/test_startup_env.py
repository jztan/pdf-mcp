"""Tests for PDF_MCP_CACHE_DIR / PDF_MCP_CACHE_TTL env-var handling."""

import os
import stat
from pathlib import Path

import pytest

from pdf_mcp.cache import PDFCache
from pdf_mcp.server import _cache_dir_from_env, _ttl_hours_from_env


class TestCacheDirEnv:
    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("PDF_MCP_CACHE_DIR", raising=False)
        assert _cache_dir_from_env() is None

    def test_empty_returns_none(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", "   ")
        assert _cache_dir_from_env() is None

    def test_absolute_path_honored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(tmp_path / "custom"))
        result = _cache_dir_from_env()
        assert result == tmp_path / "custom"

    def test_tilde_expanded(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", "~/custom-pdf-cache")
        result = _cache_dir_from_env()
        assert result is not None
        assert str(result).startswith(str(Path.home()))
        assert "custom-pdf-cache" in str(result)


class TestTtlEnv:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("PDF_MCP_CACHE_TTL", raising=False)
        assert _ttl_hours_from_env() == 24

    def test_valid_int_honored(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_TTL", "48")
        assert _ttl_hours_from_env() == 48

    def test_zero_allowed(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_TTL", "0")
        assert _ttl_hours_from_env() == 0

    def test_max_allowed(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_TTL", "8760")
        assert _ttl_hours_from_env() == 8760

    def test_non_int_fails_loud(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_TTL", "24h")
        with pytest.raises(ValueError, match="must be an integer"):
            _ttl_hours_from_env()

    def test_negative_fails_loud(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_TTL", "-1")
        with pytest.raises(ValueError, match=r"must be in \[0, 8760\]"):
            _ttl_hours_from_env()

    def test_over_max_fails_loud(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_TTL", "9999")
        with pytest.raises(ValueError, match=r"must be in \[0, 8760\]"):
            _ttl_hours_from_env()


class TestCacheDirPerms:
    def test_cache_dir_chmod_0o700(self, tmp_path):
        target = tmp_path / "pdfmcp-perm-test"
        PDFCache(cache_dir=target)
        mode = stat.S_IMODE(os.stat(target).st_mode)
        assert mode == 0o700, f"expected 0o700, got {oct(mode)}"
