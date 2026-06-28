"""
User-configurable access rules for pdf-mcp.

Loads ~/.config/pdf-mcp/config.toml (optional). Missing file = permissive.
Malformed file = ValueError at startup (never silently fall back to permissive).
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path
from typing import Any

from .embedder import DEFAULT_MODEL

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pdf-mcp" / "config.toml"

_DEFAULT_MAX_RESPONSE_BYTES = 200_000
_MAX_RESPONSE_BYTES_CEILING = 2_000_000
_MIN_RESPONSE_BYTES = 4_096


class PDFConfig:
    def __init__(self, config_path: Path | None = None) -> None:
        if config_path is None:
            config_path = _DEFAULT_CONFIG_PATH
        self._config_path = config_path
        self._data = self._load(config_path)

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            with open(path, "rb") as f:
                data: dict[str, Any] = tomllib.load(f)
                return data
        except Exception as e:
            raise ValueError(f"Failed to parse config file {path}: {e}") from e

    def check_path(self, path: str) -> None:
        """Enforce [paths] allow/deny rules. Raises ValueError if denied."""
        rules = self._data.get("paths", {})
        allow: list[str] = rules.get("allow", [])
        deny: list[str] = rules.get("deny", [])

        resolved = str(Path(path).expanduser().resolve())

        for pattern in deny:
            expanded = str(Path(pattern).expanduser())
            if fnmatch.fnmatch(resolved, expanded):
                raise ValueError(f"Path denied by config: {path}")

        if allow:
            for pattern in allow:
                expanded = str(Path(pattern).expanduser())
                if fnmatch.fnmatch(resolved, expanded):
                    return
            raise ValueError(f"Path not in allowed list: {path}")

    @property
    def embedding_model(self) -> str:
        """Return configured embedding model, or the default bge-small model."""
        model: str = self._data.get("embedding", {}).get("model", DEFAULT_MODEL)
        return model

    @property
    def max_response_bytes(self) -> int:
        """
        Maximum UTF-8 byte size of the **text content** returned by
        `pdf_read_all` (the `full_text` field) and section-granularity
        `pdf_search` (the sum of included section titles plus a per-entry
        overhead estimate). This bounds the content the cap was designed
        to bound — the field an LLM sees as untrusted PDF data.

        Note: this is NOT a wire-level envelope cap. The MCP TextContent
        block that crosses the transport also carries the other response
        fields (`truncated`, `next_page`, etc.) plus JSON framing
        overhead, typically adding ~300–500 bytes on top of this limit.
        Callers that need strict wire-size enforcement should pick a
        cap a few KB below their transport ceiling.

        Loaded from `[limits].max_response_bytes` in config.toml. Values
        above `_MAX_RESPONSE_BYTES_CEILING` are clamped down; values below
        `_MIN_RESPONSE_BYTES` are clamped up.
        """
        raw = self._data.get("limits", {}).get(
            "max_response_bytes", _DEFAULT_MAX_RESPONSE_BYTES
        )
        if not isinstance(raw, int):
            raise ValueError(
                f"[limits].max_response_bytes must be an integer, "
                f"got {type(raw).__name__}"
            )
        return max(_MIN_RESPONSE_BYTES, min(_MAX_RESPONSE_BYTES_CEILING, raw))

    @property
    def injection_phrases(self) -> tuple[str, ...]:
        """Extra hidden-text injection phrases from
        ``[content_trust].injection_phrases``. These EXTEND the built-in
        English defaults (never replace them) and enable non-English coverage.

        Returns the raw user strings; normalization happens at the matching
        site in ``content_trust`` (this module stays free of a content_trust
        import). Missing table/key -> empty tuple. A value that is not a list
        of strings raises ``ValueError`` — consistent with the
        never-silently-permissive config contract.
        """
        raw = self._data.get("content_trust", {}).get("injection_phrases", [])
        if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
            raise ValueError(
                "[content_trust].injection_phrases must be a list of strings"
            )
        return tuple(raw)

    def check_url_host(self, hostname: str) -> None:
        """Enforce [urls] allow/deny rules. Raises ValueError if denied."""
        rules = self._data.get("urls", {})
        allow: list[str] = rules.get("allow", [])
        deny: list[str] = rules.get("deny", [])

        host = hostname.lower()

        for pattern in deny:
            if fnmatch.fnmatch(host, pattern.lower()):
                raise ValueError(f"URL host denied by config: {hostname}")

        if allow:
            for pattern in allow:
                if fnmatch.fnmatch(host, pattern.lower()):
                    return
            raise ValueError(f"URL host not in allowed list: {hostname}")
