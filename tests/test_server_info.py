# tests/test_server_info.py
"""Tests for the server_info MCP tool — setup-time feature/config discovery."""

import os
from pathlib import Path

from unittest.mock import patch

from pdf_mcp import __version__
from pdf_mcp import embedder, extractor
from pdf_mcp import server
from pdf_mcp.server import server_info, _detect_features


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
        """cache_dir is the only absolute path allowed to cross the wire."""
        result = server_info()
        home = str(Path.home())
        for path, value in _string_leaves(result):
            if path == ("config", "cache_dir"):
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
