"""Tests for pdf_mcp.warm_cli (the `pdf-mcp-warm` offline prewarm entry point)."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pdf_mcp import corpus, warm_cli


class TestDiscoverPdfs:
    def test_finds_pdfs_in_directory_non_recursive(self, corpus_dir):
        found = warm_cli._discover_pdfs([str(corpus_dir)], recursive=False)
        assert len(found) == 3
        assert all(f.endswith(".pdf") for f in found)

    def test_non_recursive_skips_subdirectory(self, corpus_dir):
        sub = corpus_dir / "nested"
        sub.mkdir()
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(sub / "deep.pdf"))
        doc.close()

        found = warm_cli._discover_pdfs([str(corpus_dir)], recursive=False)
        assert not any("deep.pdf" in f for f in found)

    def test_recursive_finds_nested_pdf(self, corpus_dir):
        sub = corpus_dir / "nested"
        sub.mkdir()
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(sub / "deep.pdf"))
        doc.close()

        found = warm_cli._discover_pdfs([str(corpus_dir)], recursive=True)
        assert any("deep.pdf" in f for f in found)
        assert len(found) == 4

    def test_explicit_file_paths_are_included(self, corpus_dir):
        target = str(corpus_dir / "alpha.pdf")
        found = warm_cli._discover_pdfs([target], recursive=False)
        assert found == [target]

    def test_missing_path_is_skipped_not_fatal(self, corpus_dir, capsys):
        target = str(corpus_dir / "alpha.pdf")
        found = warm_cli._discover_pdfs(["/no/such/path", target], recursive=False)
        assert found == [target]
        assert "skip (not found)" in capsys.readouterr().err

    def test_dedupes_overlapping_inputs(self, corpus_dir):
        target = str(corpus_dir / "alpha.pdf")
        found = warm_cli._discover_pdfs([target, str(corpus_dir)], recursive=False)
        assert found.count(target) == 1

    def test_result_is_sorted(self, corpus_dir):
        found = warm_cli._discover_pdfs([str(corpus_dir)], recursive=False)
        assert found == sorted(found)


class TestChunks:
    def test_splits_into_expected_sizes(self):
        assert warm_cli._chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]

    def test_size_larger_than_list_returns_one_chunk(self):
        assert warm_cli._chunks([1, 2], 10) == [[1, 2]]

    def test_empty_list_returns_no_chunks(self):
        assert warm_cli._chunks([], 5) == []


class TestCacheEnvHelpers:
    def test_cache_dir_from_env_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("PDF_MCP_CACHE_DIR", raising=False)
        assert warm_cli._cache_dir_from_env() is None

    def test_cache_dir_from_env_expands_user(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", "~/x")
        # Path.home() / "x" rather than an absolute-path-starts-with-"/"
        # check: on Windows an expanded path starts with a drive letter,
        # not "/".
        assert warm_cli._cache_dir_from_env() == Path.home() / "x"

    def test_ttl_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("PDF_MCP_CACHE_TTL", raising=False)
        assert warm_cli._ttl_hours_from_env() == 24

    def test_ttl_reads_valid_int(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_TTL", "48")
        assert warm_cli._ttl_hours_from_env() == 48

    def test_ttl_rejects_non_integer(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_TTL", "soon")
        with pytest.raises(ValueError, match="must be an integer"):
            warm_cli._ttl_hours_from_env()

    def test_ttl_rejects_out_of_range(self, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_TTL", "999999")
        with pytest.raises(ValueError, match=r"\[0, 8760\]"):
            warm_cli._ttl_hours_from_env()


class TestMainEndToEnd:
    def test_warms_a_directory_text_only(
        self, corpus_dir, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(tmp_path / "cache"))
        rc = warm_cli.main([str(corpus_dir), "--no-embeddings", "--no-sections"])
        assert rc == 0
        out = capsys.readouterr().err
        assert "Found 3 PDF file(s)." in out
        assert "3 warmed this run" in out
        assert "0 unprocessed" in out

    def test_rerun_is_free_resume(self, corpus_dir, tmp_path, monkeypatch, capsys):
        cache_dir = str(tmp_path / "cache")
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", cache_dir)
        warm_cli.main([str(corpus_dir), "--no-embeddings"])
        capsys.readouterr()  # discard first run's output

        rc = warm_cli.main([str(corpus_dir), "--no-embeddings"])
        assert rc == 0
        out = capsys.readouterr().err
        assert "0 warmed this run" in out
        assert "0 warmed, 3 cache-verified" in out  # all 3 re-read as already cached

    def test_no_pdfs_found_returns_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(tmp_path / "cache"))
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        rc = warm_cli.main([str(empty_dir)])
        assert rc == 1

    def test_batches_split_across_multiple_warm_docs_calls(
        self, corpus_dir, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(tmp_path / "cache"))
        rc = warm_cli.main([str(corpus_dir), "--no-embeddings", "--batch-size", "1"])
        assert rc == 0
        out = capsys.readouterr().err
        assert "batch 3/3" in out

    def test_batch_size_above_max_files_is_clamped(
        self, corpus_dir, tmp_path, monkeypatch, capsys
    ):
        """Regression: resolve_corpus rejects any batch over
        corpus.CORPUS_MAX_FILES outright (its own per-call cap), so an
        unclamped --batch-size above that made every batch fail with
        "error" and every file land in skipped, exiting 1 -- with the
        clamp, this behaves exactly like the default batch size."""
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(tmp_path / "cache"))
        rc = warm_cli.main(
            [
                str(corpus_dir),
                "--no-embeddings",
                "--no-sections",
                "--batch-size",
                str(corpus.CORPUS_MAX_FILES * 10),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().err
        assert "batch 1/1" in out
        assert "3 warmed this run" in out
        assert "0 skipped" in out

    def test_embeddings_flag_warms_vectors(
        self, corpus_dir, tmp_path, monkeypatch, capsys
    ):
        """Full round trip through the real (small) fastembed model -- this
        is the one integration-shaped test in the file; the rest stay
        hermetic via --no-embeddings."""
        pytest.importorskip("fastembed")
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(tmp_path / "cache"))
        rc = warm_cli.main([str(corpus_dir)])
        assert rc == 0
        out = capsys.readouterr().err
        assert "3 warmed this run" in out

    def test_sections_warm_by_default(
        self, sample_pdf_with_toc_sections, tmp_path, monkeypatch
    ):
        """The CLI's whole point is warming everything a later query might
        need, so unlike the MCP tool (sections=False by default, to stay
        budget-conscious), the CLI builds the section index unless told
        not to."""
        from pdf_mcp.cache import PDFCache

        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(cache_dir))
        rc = warm_cli.main([sample_pdf_with_toc_sections, "--no-embeddings"])
        assert rc == 0

        cache = PDFCache(cache_dir=cache_dir, ttl_hours=1)
        assert cache.get_section_fts_coverage(sample_pdf_with_toc_sections) == 5

    def test_no_sections_flag_skips_the_index(
        self, sample_pdf_with_toc_sections, tmp_path, monkeypatch
    ):
        from pdf_mcp.cache import PDFCache

        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(cache_dir))
        rc = warm_cli.main(
            [sample_pdf_with_toc_sections, "--no-embeddings", "--no-sections"]
        )
        assert rc == 0

        cache = PDFCache(cache_dir=cache_dir, ttl_hours=1)
        assert cache.get_section_fts_coverage(sample_pdf_with_toc_sections) == 0

    def test_passes_fts_language_from_config_to_the_cache(
        self, corpus_dir, tmp_path, monkeypatch
    ):
        """A corpus warmed with `pdf-mcp-warm` must land in the German FTS
        mirror too when [fts] language = "de" is set, or a doc warmed
        offline returns empty page_match_counts / no keyword hits under a
        "de"-mode server despite being fully cached. main() reads
        fts_language off the SAME PDFConfig it already builds (for
        embedding_model / check_path) and passes it to PDFCache, exactly
        like server.py does at startup."""

        class _StubConfig:
            embedding_model = "unused"
            fts_language = "de"

            @staticmethod
            def check_path(path: str) -> None:
                return None

        monkeypatch.setattr(warm_cli, "PDFConfig", _StubConfig)
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(cache_dir))

        rc = warm_cli.main([str(corpus_dir), "--no-embeddings", "--no-sections"])
        assert rc == 0

        from pdf_mcp.cache import PDFCache

        # A plain (non-"de") re-open sees the mirror table already exists
        # AND already has rows for the warmed corpus -- i.e. warm_cli's own
        # PDFCache instance wrote it directly, not something a later "de"
        # open's sync backfilled just now.
        plain = PDFCache(cache_dir=cache_dir, ttl_hours=1)
        assert plain._de_tables_exist is True
        with plain._connect() as conn:
            (count,) = conn.execute("SELECT COUNT(*) FROM pdf_search_fts_de").fetchone()
        assert count > 0


class TestMainRemoteBackend:
    """PR #47 review follow-up: pdf-mcp-warm built its own PDFConfig but
    never called embedder.configure_remote() or the startup safety check,
    so a configured `[embedding].backend = "openai"` was silently never
    used -- every prewarm ran on local fastembed with no error or
    warning. warm_cli.main() now runs the same
    remote_embedding_check.configure_remote_backend() sequence server.py
    does at startup."""

    def _openai_config(self, tmp_path) -> "Path":
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[embedding]\nbackend = "openai"\n'
            'base_url = "http://localhost:8712/v1"\n'
            'model = "bge-small-en-v1.5"\n',
            encoding="utf-8",
        )
        return cfg

    def _stub_embedder(self, monkeypatch):
        """Hermetic stand-ins for check_available/encode -- no real
        fastembed or network I/O, matching this file's --no-embeddings-
        by-default hermetic style for every test but the one real
        fastembed round trip."""
        import numpy as np

        from pdf_mcp import embedder

        monkeypatch.setattr(embedder, "check_available", lambda model: None)
        monkeypatch.setattr(
            embedder,
            "encode",
            lambda texts, model: np.zeros((len(texts), 4), dtype=np.float32),
        )

    def test_configures_and_reports_a_passing_remote_backend(
        self, corpus_dir, tmp_path, monkeypatch, capsys
    ):
        from pdf_mcp.config import PDFConfig
        from pdf_mcp.remote_embedding_check import SafetyCheckResult

        cfg_path = self._openai_config(tmp_path)
        monkeypatch.setattr(warm_cli, "PDFConfig", lambda: PDFConfig(cfg_path))
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(tmp_path / "cache"))
        self._stub_embedder(monkeypatch)

        configure_calls = []
        monkeypatch.setattr("pdf_mcp.embedder.configure_remote", configure_calls.append)
        monkeypatch.setattr(
            "pdf_mcp.remote_embedding_check.verify_remote_backend",
            lambda spec: SafetyCheckResult(ok=True, reason="fake parity ok"),
        )

        rc = warm_cli.main([str(corpus_dir), "--no-sections"])
        assert rc == 0

        assert len(configure_calls) == 1
        assert configure_calls[0] is not None
        assert configure_calls[0].base_url == "http://localhost:8712/v1"

        out = capsys.readouterr().err
        assert "remote embedding backend passed the startup safety check" in out
        assert "fake parity ok" in out

    def test_reports_and_falls_back_on_a_failing_remote_backend(
        self, corpus_dir, tmp_path, monkeypatch, capsys
    ):
        from pdf_mcp.config import PDFConfig
        from pdf_mcp.remote_embedding_check import SafetyCheckResult

        cfg_path = self._openai_config(tmp_path)
        monkeypatch.setattr(warm_cli, "PDFConfig", lambda: PDFConfig(cfg_path))
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(tmp_path / "cache"))
        self._stub_embedder(monkeypatch)

        configure_calls = []
        monkeypatch.setattr("pdf_mcp.embedder.configure_remote", configure_calls.append)
        monkeypatch.setattr(
            "pdf_mcp.remote_embedding_check.verify_remote_backend",
            lambda spec: SafetyCheckResult(ok=False, reason="fake cosine mismatch"),
        )

        rc = warm_cli.main([str(corpus_dir), "--no-sections"])
        assert rc == 0

        assert configure_calls == [None]  # fell back to local fastembed

        out = capsys.readouterr().err
        assert "warning: remote embedding backend failed" in out
        assert "fake cosine mismatch" in out

    def test_explicit_model_flag_bypasses_remote_backend(
        self, corpus_dir, tmp_path, monkeypatch, capsys
    ):
        """--model means "use this fastembed model locally" -- a
        configured remote backend must not be silently substituted in."""
        from pdf_mcp.config import PDFConfig

        cfg_path = self._openai_config(tmp_path)
        monkeypatch.setattr(warm_cli, "PDFConfig", lambda: PDFConfig(cfg_path))
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(tmp_path / "cache"))
        self._stub_embedder(monkeypatch)

        configure_calls = []
        monkeypatch.setattr("pdf_mcp.embedder.configure_remote", configure_calls.append)

        rc = warm_cli.main(
            [str(corpus_dir), "--no-sections", "--model", "BAAI/bge-small-en-v1.5"]
        )
        assert rc == 0

        assert configure_calls == []  # configure_remote_backend never called
        out = capsys.readouterr().err
        assert "remote embedding backend" not in out

    def test_misconfigured_openai_backend_errors_cleanly(
        self, corpus_dir, tmp_path, monkeypatch, capsys
    ):
        """remote_embedding_spec validates the whole [embedding] section
        and raises ValueError on a bad config -- the CLI must report it
        with its own "error: ..." + exit 1 convention (every other config
        error here does), not a raw traceback."""
        from pdf_mcp.config import PDFConfig

        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            '[embedding]\nbackend = "openai"\n'
            'base_url = "not-a-url"\n'
            'model = "bge-small-en-v1.5"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(warm_cli, "PDFConfig", lambda: PDFConfig(cfg_path))
        monkeypatch.setenv("PDF_MCP_CACHE_DIR", str(tmp_path / "cache"))

        rc = warm_cli.main([str(corpus_dir), "--no-sections"])
        assert rc == 1

        out = capsys.readouterr().err
        assert "error:" in out
        assert "base_url" in out
