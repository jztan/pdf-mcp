"""Tests for pdf_mcp.config module."""

from pathlib import Path

import pytest

from pdf_mcp.config import PDFConfig


class TestConfigLoad:
    def test_missing_file_is_permissive(self, tmp_path):
        """Missing config file means no restrictions beyond the SSRF floor."""
        config = PDFConfig(config_path=tmp_path / "nonexistent.toml")
        config.check_path("/any/path/file.pdf")
        config.check_url_host("example.com")

    def test_malformed_toml_raises_with_file_path(self, tmp_path):
        """Malformed TOML raises ValueError mentioning the file path."""
        bad = tmp_path / "config.toml"
        bad.write_text("invalid toml [[[")
        with pytest.raises(ValueError, match="config.toml"):
            PDFConfig(config_path=bad)

    def test_valid_file_is_loaded(self, tmp_path):
        """Valid TOML file is loaded and rules applied."""
        cfg = tmp_path / "config.toml"
        cfg.write_text('[paths]\ndeny = ["/secret/**"]\n')
        config = PDFConfig(config_path=cfg)
        with pytest.raises(ValueError):
            config.check_path("/secret/file.pdf")
