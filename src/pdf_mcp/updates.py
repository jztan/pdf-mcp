"""Daily update check for Claude Desktop bundle installs.

Off unless PDF_MCP_UPDATE_CHECK is truthy (the bundle sets it) or the config
says ``[updates] check = true``; ``check = false`` always wins. pip and uvx
installs set neither, so they never make this request.

One anonymous GET of PyPI's public JSON for pdf-mcp, at most once per 24
hours, from a daemon thread that never blocks the MCP handshake. Failures
are logged (stderr) and otherwise ignored: stdout is the STDIO protocol.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

ENV_VAR = "PDF_MCP_UPDATE_CHECK"
PYPI_URL = "https://pypi.org/pypi/pdf-mcp/json"
DOWNLOAD_URL = "https://github.com/jztan/pdf-mcp/releases/latest"
CACHE_FILENAME = "update_check.json"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 3.0

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def check_enabled(
    config_value: bool | None, environ: Mapping[str, str] | None = None
) -> bool:
    if config_value is not None:
        return config_value
    env = os.environ if environ is None else environ
    return env.get(ENV_VAR, "").strip().lower() in _TRUTHY


def latest_stable(releases: dict[str, list[dict[str, Any]]]) -> str | None:
    """Newest final release with at least one non-yanked file.

    Computed from ``releases`` rather than ``info.version``, whose
    pre-release and yanked semantics PyPI's JSON API docs do not define.
    """
    best: Version | None = None
    for raw, files in releases.items():
        try:
            version = Version(raw)
        except InvalidVersion:
            continue
        if version.is_prerelease or version.is_devrelease:
            continue
        if not files or all(f.get("yanked", False) for f in files):
            continue
        if best is None or version > best:
            best = version
    return str(best) if best is not None else None


def _cache_path(cache_dir: Path) -> Path:
    return cache_dir / CACHE_FILENAME


def read_cached(cache_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(_cache_path(cache_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    latest, checked_at = data.get("latest"), data.get("checked_at")
    if not (latest is None or isinstance(latest, str)):
        return None
    if not isinstance(checked_at, (int, float)):
        return None
    return {"latest": latest, "checked_at": float(checked_at)}


def fetch_latest(timeout: float = FETCH_TIMEOUT_SECONDS) -> str | None:
    import httpx

    response = httpx.get(PYPI_URL, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return latest_stable(response.json().get("releases", {}))


def refresh_if_stale(
    cache_dir: Path,
    now: float | None = None,
    fetch: Callable[[], str | None] = fetch_latest,
) -> None:
    now = time.time() if now is None else now
    cached = read_cached(cache_dir)
    if cached is not None and now - cached["checked_at"] < CHECK_INTERVAL_SECONDS:
        return
    latest = cached["latest"] if cached else None
    try:
        latest = fetch() or latest
    except Exception as exc:  # noqa: BLE001 - any failure means "no news"
        logger.info("pdf-mcp update check failed: %s", exc)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _cache_path(cache_dir).write_text(
            json.dumps({"latest": latest, "checked_at": now}), encoding="utf-8"
        )
    except OSError as exc:
        logger.info("pdf-mcp update check could not write its cache: %s", exc)


def start_background_refresh(cache_dir: Path) -> threading.Thread:
    thread = threading.Thread(
        target=refresh_if_stale,
        args=(cache_dir,),
        name="pdf-mcp-update-check",
        daemon=True,
    )
    thread.start()
    return thread


def update_status(
    current: str, cache_dir: Path, enabled: bool
) -> dict[str, Any] | None:
    if not enabled:
        return None
    cached = read_cached(cache_dir)
    latest = cached["latest"] if cached else None
    available = False
    if latest is not None:
        try:
            available = Version(latest) > Version(current)
        except InvalidVersion:
            available = False
    checked_at = (
        datetime.fromtimestamp(cached["checked_at"], timezone.utc).isoformat()
        if cached
        else None
    )
    return {
        "current": current,
        "latest": latest,
        "update_available": available,
        "checked_at": checked_at,
        "download_url": DOWNLOAD_URL,
    }


def notice_text(status: dict[str, Any] | None) -> str:
    if not status or not status.get("update_available"):
        return ""
    return (
        f"pdf-mcp {status['latest']} is available (you have "
        f"{status['current']}). To upgrade, download the new bundle from "
        f"{status['download_url']} and double-click it."
    )
