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


class TestPathRules:
    def test_no_allow_is_permissive(self, tmp_path):
        """Empty allow list means any path is accepted (within floor)."""
        cfg = tmp_path / "config.toml"
        cfg.write_text('[paths]\ndeny = []\n')
        config = PDFConfig(config_path=cfg)
        config.check_path("/any/path/file.pdf")

    def test_allow_list_enforced(self, tmp_path):
        """Path outside allow list is rejected."""
        cfg = tmp_path / "config.toml"
        cfg.write_text('[paths]\nallow = ["/data/pdfs/**"]\n')
        config = PDFConfig(config_path=cfg)
        config.check_path("/data/pdfs/report.pdf")
        with pytest.raises(ValueError, match="not in allowed"):
            config.check_path("/home/user/private.pdf")

    def test_deny_list_enforced(self, tmp_path):
        """Path matching deny pattern is rejected."""
        cfg = tmp_path / "config.toml"
        cfg.write_text('[paths]\ndeny = ["/secret/**"]\n')
        config = PDFConfig(config_path=cfg)
        with pytest.raises(ValueError, match="denied"):
            config.check_path("/secret/file.pdf")

    def test_deny_wins_over_allow(self, tmp_path):
        """Path matching both allow and deny is denied (fail-closed)."""
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[paths]\nallow = ["/data/**"]\ndeny = ["/data/secret/**"]\n'
        )
        config = PDFConfig(config_path=cfg)
        config.check_path("/data/public/report.pdf")
        with pytest.raises(ValueError, match="denied"):
            config.check_path("/data/secret/private.pdf")

    def test_tilde_expansion(self, tmp_path):
        """~ in patterns is expanded to the home directory."""
        home = str(Path.home())
        cfg = tmp_path / "config.toml"
        cfg.write_text('[paths]\nallow = ["~/Documents/**"]\n')
        config = PDFConfig(config_path=cfg)
        config.check_path(f"{home}/Documents/report.pdf")
        with pytest.raises(ValueError, match="not in allowed"):
            config.check_path("/tmp/report.pdf")

    def test_symlink_traversal_blocked(self, tmp_path):
        """Symlink from allowed path into denied path is rejected via Path.resolve()."""
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        secret_dir = tmp_path / "secret"
        secret_dir.mkdir()
        secret_file = secret_dir / "private.pdf"
        secret_file.write_bytes(b"secret")

        link = allowed_dir / "link.pdf"
        link.symlink_to(secret_file)

        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'[paths]\nallow = ["{allowed_dir}/**"]\ndeny = ["{secret_dir}/**"]\n'
        )
        config = PDFConfig(config_path=cfg)

        with pytest.raises(ValueError, match="denied"):
            config.check_path(str(link))


class TestUrlRules:
    def test_no_allow_is_permissive(self, tmp_path):
        """No allow list means any public host is accepted."""
        config = PDFConfig(config_path=tmp_path / "none.toml")
        config.check_url_host("example.com")

    def test_wildcard_matching(self, tmp_path):
        """* in hostname pattern matches any chars including dots."""
        cfg = tmp_path / "config.toml"
        cfg.write_text('[urls]\nallow = ["*.example.com"]\n')
        config = PDFConfig(config_path=cfg)
        config.check_url_host("docs.example.com")
        with pytest.raises(ValueError, match="not in allowed"):
            config.check_url_host("evil.com")

    def test_case_insensitive(self, tmp_path):
        """Hostname matching is case-insensitive."""
        cfg = tmp_path / "config.toml"
        cfg.write_text('[urls]\ndeny = ["Evil.com"]\n')
        config = PDFConfig(config_path=cfg)
        with pytest.raises(ValueError, match="denied"):
            config.check_url_host("EVIL.COM")

    def test_deny_wins_over_allow(self, tmp_path):
        """Host matching both allow and deny is denied (fail-closed)."""
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[urls]\nallow = ["*.example.com"]\ndeny = ["bad.example.com"]\n'
        )
        config = PDFConfig(config_path=cfg)
        config.check_url_host("docs.example.com")
        with pytest.raises(ValueError, match="denied"):
            config.check_url_host("bad.example.com")
