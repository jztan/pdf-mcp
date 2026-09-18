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

from urllib.parse import urlsplit

from .embedder import DEFAULT_MODEL
from .remote_embedder import RemoteSpec, _redact_base_url
from .url_fetcher import URLFetcher

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pdf-mcp" / "config.toml"

_DEFAULT_MAX_RESPONSE_BYTES = 200_000
_MAX_RESPONSE_BYTES_CEILING = 2_000_000
_MIN_RESPONSE_BYTES = 4_096

# [embedding] defaults for the "openai" backend. Mirrors RemoteSpec's own
# defaults so a config.toml that sets only backend/base_url/model still gets
# sane values.
_DEFAULT_REMOTE_TIMEOUT = 60.0
_DEFAULT_REMOTE_BATCH_SIZE = 32
_DEFAULT_REMOTE_MAX_CONCURRENCY = 4

# Keys that only make sense for the general/arbitrary-model case (asymmetric
# document/query prefixes, an explicit output-dimension check). This narrow
# version only ever serves bge-small-en-v1.5 remotely, which needs neither --
# see remote_embedder.py's module docstring and issue #46. Rejected eagerly
# (ValueError) rather than silently ignored, matching this module's existing
# "malformed config = ValueError, never silently permissive" contract: a user
# who sets one of these almost certainly wants the general behavior this
# version deliberately does not provide.
_UNSUPPORTED_REMOTE_KEYS = ("dimensions", "document_prefix", "query_prefix")

# The startup cosine-parity safety check used to be optional
# (`[embedding].verify_startup`). It is now mandatory -- a verified remote
# endpoint writes to the SAME cache rows as local fastembed (see
# `embedding_model`), so an unverified one could silently poison those rows
# with vectors from a different model/quantization/pooling. Rejected
# (ValueError) rather than silently ignored, same contract as
# `_UNSUPPORTED_REMOTE_KEYS`: a config still setting this key needs to know
# it no longer does anything.
_REMOVED_REMOTE_KEYS = ("verify_startup",)


class PDFConfig:
    def __init__(self, config_path: Path | None = None) -> None:
        if config_path is None:
            config_path = _DEFAULT_CONFIG_PATH
        self._config_path = config_path
        self._data = self._load(config_path)
        # Set by disable_remote_embedding_backend() when the startup safety
        # check (issue #42, remote_embedding_check) decides the configured
        # remote endpoint cannot be trusted. Once set, embedding_backend,
        # embedding_model, and remote_embedding_spec all report the local
        # fastembed default for the rest of this process -- see
        # disable_remote_embedding_backend's docstring.
        self._remote_backend_disabled = False

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

    def disable_remote_embedding_backend(self) -> None:
        """Force the local fastembed default for the rest of this process.

        Called exactly once, by server.py's startup safety check
        (`remote_embedding_check`, issue #42), when a configured
        ``[embedding].backend = "openai"`` endpoint fails the cosine-parity
        check against stored fastembed reference vectors -- wrong model,
        wrong quantization, wrong pooling, or simply unreachable. After
        this call, `embedding_backend`, `embedding_model`, and
        `remote_embedding_spec` all behave exactly as if
        ``[embedding].backend`` had never been set to "openai", regardless
        of what config.toml says -- this is what makes the fallback safe:
        every call site that resolves an embedding identity from this
        object (search-capability probing, cache identity, the actual
        encode() dispatch) sees the same, consistent, local-only state, so
        nothing downstream can end up with `embedding_model` naming a
        remote identity that `embedder.configure_remote` was never told
        about. Idempotent.
        """
        self._remote_backend_disabled = True

    @property
    def embedding_backend(self) -> str:
        """``[embedding].backend``: "fastembed" (default) or "openai".

        "openai" means an OpenAI-compatible ``/v1/embeddings`` HTTP endpoint
        (ollama, lemonade, llama-server, vLLM, ...) serving bge-small-en-v1.5
        on a self-hosted server -- see remote_embedding_spec's docstring for
        why it is scoped to that one model in this version. Validated eagerly at
        load time -- this module never touches [embedding] lazily, matching
        the rest of this class.

        Reports "fastembed" unconditionally once
        `disable_remote_embedding_backend` has been called, regardless of
        what config.toml says -- see that method's docstring.
        """
        if self._remote_backend_disabled:
            return "fastembed"
        backend = self._data.get("embedding", {}).get("backend", "fastembed")
        if backend not in ("fastembed", "openai"):
            raise ValueError(
                f"[embedding].backend must be 'fastembed' or 'openai', got "
                f"{backend!r}"
            )
        return str(backend)

    @property
    def embedding_model(self) -> str:
        """
        The embedding identity string that keys the vector cache (cache.py's
        page_embeddings/doc_profiles ``model`` column) and is passed to
        embedder.encode/encode_query/check_available.

        fastembed backend (`[embedding].backend` absent or "fastembed"):
        `[embedding].model` if set, else `DEFAULT_MODEL` -- unchanged by
        this backend's existence, so no cache is invalidated by it.

        openai backend, and after `disable_remote_embedding_backend` has
        been called on an openai config: always `DEFAULT_MODEL`, NEVER
        `[embedding].model` (that key names the *remote* model label sent
        to the endpoint -- see remote_embedding_spec's docstring -- and
        would be meaningless to fastembed's local catalog if used here,
        including in the disabled/fallback case, where it may not even name
        a real fastembed model). The openai backend is scoped to serving
        that exact model remotely and its mandatory startup safety check
        (`remote_embedding_check`) proves the served vectors match local
        fastembed's within 0.999 cosine before the backend is ever used --
        so a verified remote endpoint shares the SAME cache rows as local
        fastembed, rather than a namespaced, per-endpoint identity. That is
        the point: a CPU fallback, a `127.0.0.1` vs `localhost` base_url, or
        switching which remote box serves bge-small should all read the
        same cached vectors instead of re-embedding everything.
        """
        if self._remote_backend_disabled:
            return DEFAULT_MODEL
        if self.embedding_backend == "fastembed":
            model: str = self._data.get("embedding", {}).get("model", DEFAULT_MODEL)
            return model
        return DEFAULT_MODEL

    @property
    def remote_embedding_spec(self) -> "RemoteSpec | None":
        """Parsed ``[embedding]`` config for the openai backend, or None
        when ``backend`` is "fastembed" (the default).

        This version only ever talks to a remotely-served bge-small-en-v1.5
        -- the point is a Vulkan/iGPU speed win on hardware fastembed's
        onnxruntime backends can't reach, not arbitrary model choice (see
        issue #42). `model` is accepted and forwarded to the endpoint purely
        as a label naming what it should load -- see `embedding_model`'s
        docstring for why it is NOT folded into the cache identity; nothing
        in this codebase branches on its value the way it would need to for
        a different model's prefix contract or cosine distribution (that
        generality, and the low_confidence/RRF threshold recalibration it
        requires, is tracked separately in issue #46).

        `document_prefix`, `query_prefix`, and `dimensions` are therefore
        deliberately unsupported keys here -- see `_UNSUPPORTED_REMOTE_KEYS`
        -- and raise ValueError if present, rather than being silently
        ignored, since a user who sets one of them almost certainly wants
        behavior this version does not provide.

        `api_key_env` names an environment variable; the key itself is
        never read from or written to config.toml.

        `base_url`'s hostname must resolve to a private/loopback address
        (checked via `url_fetcher.URLFetcher._is_blocked_ip`, reused the
        other way round) -- an accident-guard against sending page/query
        text to a public API by mistake, not a security boundary (won't
        catch a tunnel/VPN routing a private address to a public host). A
        hostname that fails to resolve at all is treated as "blocked" by
        `_is_blocked_ip` (fail-closed for its SSRF use), which here means
        this check passes rather than raises -- e.g. a compose service
        that hasn't started yet won't block config load.
        """
        if self.embedding_backend != "openai":
            return None
        section = self._data.get("embedding", {})

        present_unsupported = [k for k in _UNSUPPORTED_REMOTE_KEYS if k in section]
        if present_unsupported:
            raise ValueError(
                "[embedding]." + ", [embedding].".join(present_unsupported) + " "
                "are not supported with [embedding].backend = 'openai' in "
                "this version -- this backend only serves bge-small-en-v1.5 "
                "remotely, matching fastembed's default model exactly, with "
                "no prefix or dimension configuration surface. See "
                "https://github.com/jztan/pdf-mcp/issues/46 for the tracked "
                "general-model-choice follow-up."
            )

        present_removed = [k for k in _REMOVED_REMOTE_KEYS if k in section]
        if present_removed:
            raise ValueError(
                "[embedding]." + ", [embedding].".join(present_removed) + " "
                "no longer has any effect -- the startup cosine-parity "
                "safety check is now mandatory for [embedding].backend = "
                "'openai', since a verified endpoint shares cache rows "
                "with local fastembed. Remove it from config.toml."
            )

        base_url = section.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            raise ValueError(
                "[embedding].base_url is required when "
                "[embedding].backend = 'openai'"
            )
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ValueError(
                "[embedding].base_url must be an http(s) URL, got "
                f"{_redact_base_url(base_url)!r}"
            )

        hostname = urlsplit(base_url).hostname
        if hostname is None:
            raise ValueError(
                "[embedding].base_url must include a hostname, got "
                f"{_redact_base_url(base_url)!r}"
            )
        if not URLFetcher._is_blocked_ip(hostname):
            raise ValueError(
                f"[embedding].base_url {_redact_base_url(base_url)!r} "
                "resolves to a public address -- this guards against "
                "pointing pdf-mcp at a public API by accident, not a "
                "security boundary (see docs/configuration.md). Point it "
                "at a loopback, RFC 1918/link-local/CGNAT, or otherwise "
                "private-resolving address -- 'localhost', "
                "'host.docker.internal', a Docker/compose service name, a "
                "LAN hostname/IP, or a Tailscale address all resolve as "
                "private and are accepted."
            )

        model = section.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError(
                "[embedding].model is required when [embedding].backend = 'openai'"
            )

        api_key = None
        api_key_env = section.get("api_key_env")
        if api_key_env is not None:
            if not isinstance(api_key_env, str) or not api_key_env:
                raise ValueError("[embedding].api_key_env must be a non-empty string")
            import os

            api_key = os.environ.get(api_key_env)
            if api_key is None:
                raise ValueError(
                    f"[embedding].api_key_env names {api_key_env!r}, but that "
                    "environment variable is not set"
                )

        timeout = section.get("timeout", _DEFAULT_REMOTE_TIMEOUT)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError(
                f"[embedding].timeout must be a positive number, got {timeout!r}"
            )
        if timeout <= 0:
            raise ValueError(
                f"[embedding].timeout must be a positive number, got {timeout!r}"
            )

        batch_size = section.get("batch_size", _DEFAULT_REMOTE_BATCH_SIZE)
        if not isinstance(batch_size, int) or isinstance(batch_size, bool):
            raise ValueError(
                f"[embedding].batch_size must be a positive integer, got "
                f"{batch_size!r}"
            )
        if batch_size <= 0:
            raise ValueError(
                f"[embedding].batch_size must be a positive integer, got "
                f"{batch_size!r}"
            )

        max_concurrency = section.get(
            "max_concurrency", _DEFAULT_REMOTE_MAX_CONCURRENCY
        )
        if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool):
            raise ValueError(
                "[embedding].max_concurrency must be a positive integer, got "
                f"{max_concurrency!r}"
            )
        if max_concurrency <= 0:
            raise ValueError(
                "[embedding].max_concurrency must be a positive integer, got "
                f"{max_concurrency!r}"
            )

        return RemoteSpec(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout=float(timeout),
            batch_size=batch_size,
            max_concurrency=max_concurrency,
        )

    @property
    def fts_language(self) -> str | None:
        """``[fts] language``: None (default, porter/English stemming) or
        "de" for the German-stemmed FTS mirror index (cache.py's
        pdf_search_fts_de / pdf_section_fts_de tables).

        This is a whole-cache setting, not per-document: it applies to every
        PDF the running server touches, the same way `embedding_model` does.
        A user who mostly reads German documents turns it on once; mixed
        English/German corpora are not distinguished (that would need
        per-document language detection, which this option deliberately
        does not attempt).
        """
        value = self._data.get("fts", {}).get("language")
        if value is None:
            return None
        if value == "de":
            return "de"
        raise ValueError(
            f"[fts] language must be 'de' (or omitted) in {self._config_path}, "
            f"got {value!r}"
        )

    @property
    def ocr_auto_install(self) -> bool | None:
        """``[ocr] auto_install``: None when absent. Malformed values fail loudly."""
        value = self._data.get("ocr", {}).get("auto_install")
        if value is None or isinstance(value, bool):
            return value
        raise ValueError(
            f"[ocr] auto_install must be true or false in {self._config_path}, "
            f"got {value!r}"
        )

    @property
    def update_check(self) -> bool | None:
        """``[updates] check``: None when absent. Malformed values fail loudly."""
        value = self._data.get("updates", {}).get("check")
        if value is None or isinstance(value, bool):
            return value
        raise ValueError(
            f"[updates] check must be true or false in {self._config_path}, "
            f"got {value!r}"
        )

    @property
    def config_path(self) -> Path:
        """Path this config was loaded from (may not exist)."""
        return self._config_path

    @property
    def has_path_allowlist(self) -> bool:
        """
        True when [paths] allow is a non-empty list.

        An absent or empty allow list disables only the allow gate in
        check_path; [paths] deny rules still apply unconditionally. A
        deny-only posture is the correct default for a local install and
        insufficient for a remote one, so main_http() refuses to start
        when this is False.
        """
        allow = self._data.get("paths", {}).get("allow", [])
        return isinstance(allow, list) and len(allow) > 0

    @property
    def path_allow_patterns(self) -> tuple[str, ...]:
        """
        Configured [paths] allow globs, verbatim and unexpanded.

        Reported by server_info so a caller can see which corpus the
        server is willing to open. Returned as-is (no ~ expansion, no
        glob resolution): check_path is the authority on what these
        mean, and a presentation layer that reinterpreted them could
        drift from it.
        """
        allow = self._data.get("paths", {}).get("allow", [])
        if not isinstance(allow, list):
            return ()
        return tuple(str(p) for p in allow)

    @property
    def path_deny_patterns(self) -> tuple[str, ...]:
        """
        Configured [paths] deny globs, verbatim and unexpanded.

        Deny applies unconditionally, including when there is no allow
        list, so this is meaningful in both access modes.
        """
        deny = self._data.get("paths", {}).get("deny", [])
        if not isinstance(deny, list):
            return ()
        return tuple(str(p) for p in deny)

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
