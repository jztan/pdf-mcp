# tests/test_server_info.py
"""Tests for the server_info MCP tool — setup-time feature/config discovery."""

import json
import os
from pathlib import Path

from unittest.mock import patch

from pdf_mcp import __version__
from pdf_mcp import embedder, extractor
from pdf_mcp import server
from pdf_mcp.config import PDFConfig
from pdf_mcp.server import server_info, _detect_features, _document_roots


def _string_leaves(obj, path=()):
    """Yield (path_tuple, value) for every string leaf in a nested structure."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _string_leaves(value, path + (str(key),))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _string_leaves(value, path + (str(i),))
    elif isinstance(obj, str):
        yield path, obj


class TestServerInfo:
    def test_server_info_returns_required_keys(self):
        """Response carries the three top-level keys."""
        result = server_info()
        assert "version" in result
        assert "features" in result
        assert "config" in result
        assert result["version"] == __version__

    def test_server_info_column_aware_matches_extractor(self):
        """column_aware.available is the single source of truth the extractor uses."""
        # Real agreement: the cached startup value reflects the extractor's
        # actual capability predicate.
        result = server_info()
        available = result["features"]["extraction"]["column_aware"]["available"]
        assert available == extractor.column_detection_available()

        # Mock both states: the detection logic follows the predicate, no drift.
        for state in (True, False):
            with patch.object(
                extractor, "column_detection_available", return_value=state
            ):
                feats = _detect_features()
                assert feats["extraction"]["column_aware"]["available"] is state

    def test_server_info_vertical_aware_matches_extractor(self):
        """vertical_aware.available mirrors the extractor's capability predicate."""
        result = server_info()
        vertical = result["features"]["extraction"]["vertical_aware"]
        assert vertical["available"] is True
        assert vertical["available"] == extractor.vertical_detection_available()

        feats = _detect_features()
        assert feats["extraction"]["vertical_aware"]["available"] is True

    def test_server_info_semantic_mode_iff_fastembed(self):
        """No fastembed -> modes_available is ['keyword'] and no embedding_model."""
        with patch.object(
            embedder, "check_available", side_effect=ImportError("no fastembed")
        ):
            feats = _detect_features()
        search = feats["search"]
        assert search["modes_available"] == ["keyword"]
        assert "embedding_model" not in search

        # And when fastembed loads cleanly, semantic + auto appear with a model.
        with patch.object(embedder, "check_available", return_value=None):
            feats = _detect_features()
        search = feats["search"]
        assert "semantic" in search["modes_available"]
        assert "auto" in search["modes_available"]
        assert search["embedding_model"]

    def test_server_info_no_unexpected_absolute_paths(self):
        """Only cache_dir and the documents block may carry absolute paths.

        The documents block is a deliberate widening of the original
        "cache_dir only" rule: telling a caller which roots it may read is
        the whole point of the block, and it cannot be done without naming
        them. Everything else still has to stay path-free, so the exemption
        is enumerated rather than turned into a prefix match.
        """
        result = server_info()
        home = str(Path.home())
        exempt = {("config", "cache_dir")}
        for path, value in _string_leaves(result):
            if path in exempt or path[:1] == ("documents",):
                continue
            assert not value.startswith(os.sep), (path, value)
            assert not value.startswith(home), (path, value)

    def test_server_info_config_values(self):
        """config block reports resolved worker/byte/ttl/cache-dir values."""
        result = server_info()
        cfg = result["config"]
        assert isinstance(cfg["max_workers"], int) and cfg["max_workers"] >= 1
        assert cfg["max_response_bytes"] == server.pdf_config.max_response_bytes
        assert cfg["cache_ttl_hours"] == server.cache.ttl_hours
        assert cfg["cache_dir"] == str(server.cache.cache_dir)

    def test_server_info_corpus_block(self):
        """corpus feature block advertises the cap, budget clamp, and
        the tools; clients discover the corpus feature via server_info."""
        from pdf_mcp.corpus import CORPUS_MAX_FILES

        result = server_info()
        corpus_feat = result["features"]["corpus"]
        assert corpus_feat["max_files"] == CORPUS_MAX_FILES
        assert corpus_feat["budget_seconds_range"] == [1, 300]
        assert corpus_feat["tools"] == [
            "pdf_corpus_warm",
            "pdf_corpus_overview",
            "pdf_corpus_search",
        ]

    def test_server_info_corpus_modes_track_search_modes(self):
        """Corpus search mode availability mirrors single-doc search
        (both depend on the same embedding availability)."""
        with patch.object(
            embedder, "check_available", side_effect=ImportError("no fastembed")
        ):
            feats = _detect_features()
        assert feats["corpus"]["modes_available"] == ["keyword"]

        with patch.object(embedder, "check_available", return_value=None):
            feats = _detect_features()
        assert feats["corpus"]["modes_available"] == ["keyword", "semantic", "auto"]


class TestDocumentRoots:
    """Root derivation: allow globs reduced to directories a caller can use."""

    def test_bare_directory_pattern(self, tmp_path):
        assert _document_roots((str(tmp_path),)) == [str(tmp_path.resolve())]

    def test_recursive_glob_suffix(self, tmp_path):
        """`/data/pdfs/**` yields `/data/pdfs`, the arg corpus tools want."""
        assert _document_roots((f"{tmp_path}/**",)) == [str(tmp_path.resolve())]

    def test_extension_glob_falls_back_to_parent(self, tmp_path):
        """`~/Documents/*.pdf` has no literal dir tail; the parent is the root."""
        assert _document_roots((f"{tmp_path}/*.pdf",)) == [str(tmp_path.resolve())]

    def test_nonexistent_path_is_dropped(self, tmp_path):
        """A stale config entry must not be advertised as a usable root."""
        assert _document_roots((str(tmp_path / "gone" / "**"),)) == []

    def test_home_tilde_is_expanded(self):
        """`~` is expanded, and the result is a real absolute directory."""
        roots = _document_roots(("~/**",))
        assert roots == [str(Path.home().resolve())]

    def test_duplicates_collapse_and_output_is_sorted(self, tmp_path):
        """Two patterns over one tree yield one root; order is deterministic."""
        a = tmp_path / "alpha"
        b = tmp_path / "beta"
        a.mkdir()
        b.mkdir()
        roots = _document_roots((f"{b}/**", f"{a}/*.pdf", str(a)))
        assert roots == [str(a.resolve()), str(b.resolve())]

    def test_file_pattern_keeps_containing_directory(self, tmp_path):
        """An exact-file allow rule still tells the caller where to look."""
        target = tmp_path / "report.pdf"
        target.write_bytes(b"%PDF-1.4\n")
        assert _document_roots((str(target),)) == [str(tmp_path.resolve())]

    def test_empty_patterns_yield_no_roots(self):
        assert _document_roots(()) == []


class TestServerInfoDocumentsBlock:
    """The documents block is how a caller finds a path to start from."""

    def _with_paths_config(self, tmp_path, monkeypatch, allow=None, deny=None):
        """Point server.pdf_config at a config carrying the given rules."""
        body = "[paths]\n"
        if allow is not None:
            body += f"allow = {allow!r}\n".replace("'", '"')
        if deny is not None:
            body += f"deny = {deny!r}\n".replace("'", '"')
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(body, encoding="utf-8")
        monkeypatch.setattr(server, "pdf_config", PDFConfig(cfg_file))

    def test_allowlist_mode_reports_usable_roots(self, tmp_path, monkeypatch):
        corpus = tmp_path / "pdfs"
        corpus.mkdir()
        self._with_paths_config(tmp_path, monkeypatch, allow=[f"{corpus}/**"])

        docs = server_info()["documents"]
        assert docs["access_mode"] == "allowlist"
        assert docs["roots"] == [str(corpus.resolve())]
        assert docs["allow_patterns"] == [f"{corpus}/**"]
        assert docs["deny_patterns"] == []

    def test_unrestricted_mode_has_empty_roots(self, tmp_path, monkeypatch):
        """No allow list means "any readable path", not "no documents"."""
        self._with_paths_config(tmp_path, monkeypatch, deny=["/etc/**"])

        docs = server_info()["documents"]
        assert docs["access_mode"] == "unrestricted"
        assert docs["roots"] == []
        assert docs["allow_patterns"] == []

    def test_deny_patterns_reported_in_both_modes(self, tmp_path, monkeypatch):
        """Deny applies unconditionally, so it is reported unconditionally."""
        corpus = tmp_path / "pdfs"
        corpus.mkdir()
        for allow in (None, [f"{corpus}/**"]):
            self._with_paths_config(
                tmp_path, monkeypatch, allow=allow, deny=["**/secret/**"]
            )
            assert server_info()["documents"]["deny_patterns"] == ["**/secret/**"]

    def test_json_serialisable(self, tmp_path, monkeypatch):
        """Tuples from the config must not leak into the response."""
        corpus = tmp_path / "pdfs"
        corpus.mkdir()
        self._with_paths_config(tmp_path, monkeypatch, allow=[f"{corpus}/**"])
        docs = server_info()["documents"]
        assert json.loads(json.dumps(docs)) == docs


class TestDocumentRootsAreConsumable:
    """The point of `roots` is that a caller can use one without editing it.

    Asserting the key exists would not catch the failure this guards: a root
    reported as the glob `/corpus/**`, or with a trailing separator, keys off
    a different string than the corpus tools resolve. The 2026-07-25
    corpus/FTS5 incident came from exactly that shape of parity claim, one
    that held on key sets and failed on values.
    """

    def test_reported_root_resolves_through_pdf_corpus_overview(
        self, corpus_dir, tmp_path, monkeypatch, isolated_server
    ):
        from pdf_mcp.server import pdf_corpus_overview

        cfg_file = tmp_path / "config.toml"
        # as_posix(): a raw Windows path makes TOML read its
        # backslashes as escapes ("Invalid hex value" on \Users).
        cfg_file.write_text(
            f'[paths]\nallow = ["{Path(corpus_dir).as_posix()}/**"]\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(server, "pdf_config", PDFConfig(cfg_file))

        roots = server_info()["documents"]["roots"]
        assert roots, "an allow-listed existing directory must yield a root"

        # Pass it through verbatim, exactly as an agent would.
        result = pdf_corpus_overview(roots[0])
        assert "error" not in result, result
        assert len(result["docs"]) == 3

    def test_reported_root_passes_the_allow_list_it_came_from(
        self, corpus_dir, tmp_path, monkeypatch
    ):
        """A root must never name a directory check_path would refuse."""
        cfg_file = tmp_path / "config.toml"
        # as_posix(): a raw Windows path makes TOML read its
        # backslashes as escapes ("Invalid hex value" on \Users).
        cfg_file.write_text(
            f'[paths]\nallow = ["{Path(corpus_dir).as_posix()}/**"]\n',
            encoding="utf-8",
        )
        config = PDFConfig(cfg_file)
        monkeypatch.setattr(server, "pdf_config", config)

        for root in server_info()["documents"]["roots"]:
            # check_path matches resolved FILE paths, so probe with a member.
            config.check_path(str(Path(root) / "alpha.pdf"))
