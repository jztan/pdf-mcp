"""Tests for pdf_mcp.config module."""

from pathlib import Path

import pytest

from pdf_mcp.config import PDFConfig
from pdf_mcp.embedder import DEFAULT_MODEL


class TestConfigLoad:
    def test_missing_file_is_permissive(self, tmp_path):
        """Missing config file means no restrictions beyond the SSRF floor."""
        config = PDFConfig(config_path=tmp_path / "nonexistent.toml")
        config.check_path("/any/path/file.pdf")
        config.check_url_host("example.com")

    def test_malformed_toml_raises_with_file_path(self, tmp_path):
        """Malformed TOML raises ValueError mentioning the file path."""
        bad = tmp_path / "config.toml"
        bad.write_text("invalid toml [[[", encoding="utf-8")
        with pytest.raises(ValueError, match="config.toml"):
            PDFConfig(config_path=bad)

    def test_valid_file_is_loaded(self, tmp_path):
        """Valid TOML file is loaded and rules applied."""
        cfg = tmp_path / "config.toml"
        secret = tmp_path / "secret"
        cfg.write_text(
            f'[paths]\ndeny = ["{secret.as_posix()}/**"]\n', encoding="utf-8"
        )
        config = PDFConfig(config_path=cfg)
        with pytest.raises(ValueError):
            config.check_path(str(secret / "file.pdf"))


class TestPathRules:
    def test_no_allow_is_permissive(self, tmp_path):
        """Empty allow list means any path is accepted (within floor)."""
        cfg = tmp_path / "config.toml"
        cfg.write_text("[paths]\ndeny = []\n", encoding="utf-8")
        config = PDFConfig(config_path=cfg)
        config.check_path("/any/path/file.pdf")

    def test_allow_list_enforced(self, tmp_path):
        """Path outside allow list is rejected."""
        cfg = tmp_path / "config.toml"
        pdfs = tmp_path / "data" / "pdfs"
        cfg.write_text(f'[paths]\nallow = ["{pdfs.as_posix()}/**"]\n', encoding="utf-8")
        config = PDFConfig(config_path=cfg)
        config.check_path(str(pdfs / "report.pdf"))
        with pytest.raises(ValueError, match="not in allowed"):
            config.check_path(str(tmp_path / "home" / "private.pdf"))

    def test_deny_list_enforced(self, tmp_path):
        """Path matching deny pattern is rejected."""
        cfg = tmp_path / "config.toml"
        secret = tmp_path / "secret"
        cfg.write_text(
            f'[paths]\ndeny = ["{secret.as_posix()}/**"]\n', encoding="utf-8"
        )
        config = PDFConfig(config_path=cfg)
        with pytest.raises(ValueError, match="denied"):
            config.check_path(str(secret / "file.pdf"))

    def test_deny_wins_over_allow(self, tmp_path):
        """Path matching both allow and deny is denied (fail-closed)."""
        cfg = tmp_path / "config.toml"
        data = tmp_path / "data"
        cfg.write_text(
            f'[paths]\nallow = ["{data.as_posix()}/**"]\n'
            f'deny = ["{(data / "secret").as_posix()}/**"]\n',
            encoding="utf-8",
        )
        config = PDFConfig(config_path=cfg)
        config.check_path(str(data / "public" / "report.pdf"))
        with pytest.raises(ValueError, match="denied"):
            config.check_path(str(data / "secret" / "private.pdf"))

    def test_tilde_expansion(self, tmp_path):
        """~ in patterns is expanded to the home directory."""
        home = str(Path.home())
        cfg = tmp_path / "config.toml"
        cfg.write_text('[paths]\nallow = ["~/Documents/**"]\n', encoding="utf-8")
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
            # as_posix(): a Windows path interpolated raw makes TOML read
            # its backslashes as escapes ("Invalid hex value" on \Users).
            # fnmatch normalises separators on Windows, so forward slashes
            # match either way.
            f'[paths]\nallow = ["{allowed_dir.as_posix()}/**"]\n'
            f'deny = ["{secret_dir.as_posix()}/**"]\n',
            encoding="utf-8",
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
        cfg.write_text('[urls]\nallow = ["*.example.com"]\n', encoding="utf-8")
        config = PDFConfig(config_path=cfg)
        config.check_url_host("docs.example.com")
        with pytest.raises(ValueError, match="not in allowed"):
            config.check_url_host("evil.com")

    def test_case_insensitive(self, tmp_path):
        """Hostname matching is case-insensitive."""
        cfg = tmp_path / "config.toml"
        cfg.write_text('[urls]\ndeny = ["Evil.com"]\n', encoding="utf-8")
        config = PDFConfig(config_path=cfg)
        with pytest.raises(ValueError, match="denied"):
            config.check_url_host("EVIL.COM")

    def test_deny_wins_over_allow(self, tmp_path):
        """Host matching both allow and deny is denied (fail-closed)."""
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[urls]\nallow = ["*.example.com"]\ndeny = ["bad.example.com"]\n',
            encoding="utf-8",
        )
        config = PDFConfig(config_path=cfg)
        config.check_url_host("docs.example.com")
        with pytest.raises(ValueError, match="denied"):
            config.check_url_host("bad.example.com")


def test_max_response_bytes_default_when_missing(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    cfg = PDFConfig(config_path=cfg_path)
    assert cfg.max_response_bytes == 200_000


def test_max_response_bytes_honored(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[limits]\nmax_response_bytes = 50000\n", encoding="utf-8")
    cfg = PDFConfig(config_path=cfg_path)
    assert cfg.max_response_bytes == 50_000


def test_max_response_bytes_clamped_to_ceiling(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[limits]\nmax_response_bytes = 999999999\n", encoding="utf-8")
    cfg = PDFConfig(config_path=cfg_path)
    assert cfg.max_response_bytes == 2_000_000


def test_max_response_bytes_clamped_to_floor(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[limits]\nmax_response_bytes = 10\n", encoding="utf-8")
    cfg = PDFConfig(config_path=cfg_path)
    assert cfg.max_response_bytes == 4_096


def test_max_response_bytes_rejects_non_int(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[limits]\nmax_response_bytes = "200kb"\n', encoding="utf-8")
    cfg = PDFConfig(config_path=cfg_path)
    with pytest.raises(ValueError, match="must be an integer"):
        cfg.max_response_bytes


def test_injection_phrases_default_empty_when_missing(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    cfg = PDFConfig(config_path=cfg_path)
    assert cfg.injection_phrases == ()


def test_injection_phrases_loaded_raw(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[content_trust]\n"
        'injection_phrases = ["忽略以上所有指示", '
        '"ignorez les instructions"]\n',
        # Explicit: the default encoding is cp1252 on Windows, which
        # cannot represent these phrases, and the config loader reads the
        # file as UTF-8 regardless of platform.
        encoding="utf-8",
    )
    cfg = PDFConfig(config_path=cfg_path)
    assert cfg.injection_phrases == (
        "忽略以上所有指示",
        "ignorez les instructions",
    )


def test_injection_phrases_rejects_non_list(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[content_trust]\ninjection_phrases = "not a list"\n', encoding="utf-8"
    )
    cfg = PDFConfig(config_path=cfg_path)
    with pytest.raises(ValueError, match="must be a list of strings"):
        cfg.injection_phrases


def test_injection_phrases_rejects_non_string_element(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[content_trust]\ninjection_phrases = ["ok", 123]\n', encoding="utf-8"
    )
    cfg = PDFConfig(config_path=cfg_path)
    with pytest.raises(ValueError, match="must be a list of strings"):
        cfg.injection_phrases


class TestPathAllowlistIntrospection:
    def test_missing_file_has_no_allowlist(self, tmp_path):
        config = PDFConfig(config_path=tmp_path / "nonexistent.toml")
        assert config.has_path_allowlist is False

    def test_empty_allow_list_does_not_count(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("[paths]\nallow = []\n", encoding="utf-8")
        assert PDFConfig(config_path=cfg).has_path_allowlist is False

    def test_deny_only_does_not_count(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[paths]\ndeny = ["/secret/**"]\n', encoding="utf-8")
        assert PDFConfig(config_path=cfg).has_path_allowlist is False

    def test_non_empty_allow_list_counts(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[paths]\nallow = ["/data/pdfs/**"]\n', encoding="utf-8")
        assert PDFConfig(config_path=cfg).has_path_allowlist is True

    def test_config_path_is_exposed(self, tmp_path):
        cfg = tmp_path / "config.toml"
        assert PDFConfig(config_path=cfg).config_path == cfg


class TestUpdateCheckConfig:
    def test_absent_is_none(self, tmp_path):
        assert PDFConfig(config_path=tmp_path / "none.toml").update_check is None

    def test_false_is_read(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("[updates]\ncheck = false\n", encoding="utf-8")
        assert PDFConfig(config_path=cfg).update_check is False

    def test_non_bool_raises(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[updates]\ncheck = "no"\n', encoding="utf-8")
        with pytest.raises(ValueError, match=r"\[updates\] check"):
            PDFConfig(config_path=cfg).update_check


class TestEmbeddingBackend:
    def test_default_is_fastembed(self, tmp_path):
        config = PDFConfig(config_path=tmp_path / "none.toml")
        assert config.embedding_backend == "fastembed"
        assert config.remote_embedding_spec is None

    def test_fastembed_model_name_unchanged(self, tmp_path):
        """No cache should be invalidated by this backend existing."""
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[embedding]\nmodel = "BAAI/bge-base-en-v1.5"\n', encoding="utf-8"
        )
        config = PDFConfig(config_path=cfg)
        assert config.embedding_backend == "fastembed"
        assert config.embedding_model == "BAAI/bge-base-en-v1.5"

    def test_invalid_backend_raises(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[embedding]\nbackend = "bogus"\n', encoding="utf-8")
        with pytest.raises(ValueError, match=r"\[embedding\].backend"):
            PDFConfig(config_path=cfg).embedding_backend

    def _write(self, tmp_path, body: str) -> PDFConfig:
        cfg = tmp_path / "config.toml"
        cfg.write_text(body, encoding="utf-8")
        return PDFConfig(config_path=cfg)

    def test_openai_backend_requires_base_url(self, tmp_path):
        config = self._write(
            tmp_path, '[embedding]\nbackend = "openai"\nmodel = "bge-small-en-v1.5"\n'
        )
        with pytest.raises(ValueError, match="base_url is required"):
            config.remote_embedding_spec

    def test_openai_backend_requires_model(self, tmp_path):
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\nbase_url = "http://localhost:8000/v1"\n',
        )
        with pytest.raises(ValueError, match="model is required"):
            config.remote_embedding_spec

    def test_openai_backend_rejects_non_http_base_url(self, tmp_path):
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\nbase_url = "ftp://x"\n'
            'model = "bge-small-en-v1.5"\n',
        )
        with pytest.raises(ValueError, match="http\\(s\\) URL"):
            config.remote_embedding_spec

    def test_openai_backend_rejects_public_base_url(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("203.0.113.5", 0))],
        )
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://public.example.com:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n',
        )
        with pytest.raises(ValueError, match="resolves to a public address"):
            config.remote_embedding_spec

    def test_openai_backend_accepts_private_resolving_hostname(
        self, tmp_path, monkeypatch
    ):
        """Covers the host.docker.internal / LAN-hostname case jztan asked
        for -- the check resolves the hostname first, so any name that
        lands on an RFC 1918 / link-local address passes."""
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("192.168.1.50", 0))],
        )
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://gpu-box.lan:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n',
        )
        spec = config.remote_embedding_spec
        assert spec is not None
        assert spec.base_url == "http://gpu-box.lan:8000/v1"

    def test_openai_backend_accepts_cgnat_resolving_hostname(
        self, tmp_path, monkeypatch
    ):
        """Covers the Tailscale case jztan flagged -- it hands out IPv4
        addresses from 100.64.0.0/10 (CGNAT space), which must pass the
        same as any other private-resolving hostname."""
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("100.100.100.100", 0))],
        )
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://gpu-box.tailnet.ts.net:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n',
        )
        spec = config.remote_embedding_spec
        assert spec is not None
        assert spec.base_url == "http://gpu-box.tailnet.ts.net:8000/v1"

    def test_openai_backend_accepts_loopback_literal(self, tmp_path):
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://127.0.0.1:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n',
        )
        spec = config.remote_embedding_spec
        assert spec is not None

    def test_openai_backend_rejects_public_base_url_without_leaking_credentials(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("203.0.113.5", 0))],
        )
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://user:hunter2@public.example.com:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n',
        )
        with pytest.raises(ValueError) as exc_info:
            config.remote_embedding_spec
        assert "hunter2" not in str(exc_info.value)

    def test_openai_backend_rejects_base_url_with_no_hostname(self, tmp_path):
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\nbase_url = "http:///v1"\n'
            'model = "bge-small-en-v1.5"\n',
        )
        with pytest.raises(ValueError, match="must include a hostname"):
            config.remote_embedding_spec

    def test_openai_backend_minimal_config(self, tmp_path):
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n',
        )
        spec = config.remote_embedding_spec
        assert spec is not None
        assert spec.base_url == "http://localhost:8000/v1"
        assert spec.model == "bge-small-en-v1.5"
        assert spec.api_key is None
        assert spec.timeout == 60.0
        assert spec.batch_size == 32
        assert spec.max_concurrency == 4

    def test_openai_backend_full_config(self, tmp_path):
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n'
            "timeout = 30\n"
            "batch_size = 8\n"
            "max_concurrency = 2\n",
        )
        spec = config.remote_embedding_spec
        assert spec is not None
        assert spec.timeout == 30.0
        assert spec.batch_size == 8
        assert spec.max_concurrency == 2

    def test_openai_backend_api_key_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_EMBED_KEY", "sk-secret-value")
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n'
            'api_key_env = "MY_EMBED_KEY"\n',
        )
        spec = config.remote_embedding_spec
        assert spec is not None
        assert spec.api_key == "sk-secret-value"
        # the key itself never appears in the config.toml the fixture wrote
        assert "sk-secret-value" not in config.config_path.read_text()

    def test_openai_backend_missing_api_key_env_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING_EMBED_KEY", raising=False)
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n'
            'api_key_env = "MISSING_EMBED_KEY"\n',
        )
        with pytest.raises(ValueError, match="MISSING_EMBED_KEY"):
            config.remote_embedding_spec

    @pytest.mark.parametrize(
        "bad_key",
        [
            "dimensions = 384",
            'document_prefix = "passage: "',
            'query_prefix = "query: "',
        ],
    )
    def test_openai_backend_rejects_model_choice_keys(self, tmp_path, bad_key):
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n' + bad_key + "\n",
        )
        with pytest.raises(ValueError, match="not supported"):
            config.remote_embedding_spec

    def test_openai_backend_rejects_bad_timeout(self, tmp_path):
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n'
            "timeout = -1\n",
        )
        with pytest.raises(ValueError, match="timeout"):
            config.remote_embedding_spec

    def test_openai_backend_rejects_bad_batch_size(self, tmp_path):
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n'
            "batch_size = 0\n",
        )
        with pytest.raises(ValueError, match="batch_size"):
            config.remote_embedding_spec

    def test_embedding_model_for_openai_backend_shares_fastembed_identity(
        self, tmp_path
    ):
        """PR #47 review item 2: a verified remote endpoint shares the SAME
        cache rows as local fastembed, so embedding_model is no longer
        namespaced per host/model under the openai backend."""
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n',
        )
        assert config.embedding_model == DEFAULT_MODEL

    def test_openai_backend_rejects_verify_startup_key(self, tmp_path):
        """PR #47 review item 2: the cosine-parity check is now mandatory,
        so a config still setting the removed [embedding].verify_startup
        key must fail loudly rather than silently do nothing."""
        config = self._write(
            tmp_path,
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n'
            "verify_startup = false\n",
        )
        with pytest.raises(ValueError, match="verify_startup"):
            config.remote_embedding_spec


class TestDisableRemoteEmbeddingBackend:
    """The startup safety-check fallback (issue #42) -- exercised directly
    here since server.py only calls it at import time, where a broken
    fallback would ship green (no other test covers this path)."""

    def _openai_config(self, tmp_path) -> PDFConfig:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n',
            encoding="utf-8",
        )
        return PDFConfig(config_path=cfg)

    def test_forces_fastembed_backend_and_default_model(self, tmp_path):
        config = self._openai_config(tmp_path)
        assert config.embedding_backend == "openai"

        config.disable_remote_embedding_backend()

        assert config.embedding_backend == "fastembed"
        assert config.embedding_model == DEFAULT_MODEL
        assert config.remote_embedding_spec is None

    def test_is_idempotent(self, tmp_path):
        config = self._openai_config(tmp_path)
        config.disable_remote_embedding_backend()
        config.disable_remote_embedding_backend()
        assert config.embedding_backend == "fastembed"

    def test_does_not_affect_a_fresh_config_instance(self, tmp_path):
        """Disabling one PDFConfig instance must not be global state that
        leaks into a different instance (e.g. a second PDFConfig() built
        for a test or a different process)."""
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'model = "bge-small-en-v1.5"\n',
            encoding="utf-8",
        )
        disabled = PDFConfig(config_path=cfg_path)
        disabled.disable_remote_embedding_backend()

        fresh = PDFConfig(config_path=cfg_path)
        assert fresh.embedding_backend == "openai"
        assert fresh.remote_embedding_spec is not None


class TestFtsLanguageConfig:
    def test_absent_is_none(self, tmp_path):
        assert PDFConfig(config_path=tmp_path / "none.toml").fts_language is None

    def test_de_is_read(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[fts]\nlanguage = "de"\n', encoding="utf-8")
        assert PDFConfig(config_path=cfg).fts_language == "de"

    def test_unsupported_value_raises(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[fts]\nlanguage = "fr"\n', encoding="utf-8")
        with pytest.raises(ValueError, match=r"\[fts\] language"):
            PDFConfig(config_path=cfg).fts_language
