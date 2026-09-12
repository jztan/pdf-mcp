"""Zero-install OCR: fetch a pinned, portable Tesseract on first use.

On only for Claude Desktop bundle installs (the bundle's shim sets
PDF_MCP_OCR_AUTO_INSTALL=1); ``[ocr] auto_install`` in the config always
wins. A Tesseract the user installed is always preferred: find_tesseract()
looks here last. The zip for this platform comes from a jztan/pdf-mcp-
tesseract release, is checked against a SHA-256 pinned in this package
before anything is unpacked, and is unpacked safely into
<cache>/tesseract/<tag>/.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

ENV_VAR = "PDF_MCP_OCR_AUTO_INSTALL"
PINS_PATH = Path(__file__).with_name("tesseract_pins.json")
DOWNLOAD_BASE = "https://github.com/jztan/pdf-mcp-tesseract/releases/download"
#: How long an OCR call waits for a first download (~14 MB) before replying
#: "setting up"; the download carries on in the background.
WAIT_SECONDS = 20.0
TIMEOUT_SECONDS = 60.0

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_cache_dir: Path | None = None
_lock = threading.Lock()
_thread: threading.Thread | None = None
#: Set after a hash mismatch: no retry until the server restarts.
_failed: str | None = None
_last_error: str | None = None


class IntegrityError(Exception):
    """The download is not the pinned file, or its zip is unsafe."""


def configure(cache_dir: Path) -> None:
    """Called once by the server with its cache directory."""
    global _cache_dir  # noqa: PLW0603
    _cache_dir = Path(cache_dir)


def enabled(
    config_value: bool | None, environ: Mapping[str, str] | None = None
) -> bool:
    if config_value is not None:
        return config_value
    env = os.environ if environ is None else environ
    return env.get(ENV_VAR, "").strip().lower() in _TRUTHY


def platform_key(system: str | None = None, machine: str | None = None) -> str | None:
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}
    os_key = {"win32": "win32", "darwin": "darwin"}.get(system)
    key = f"{os_key}-{arch.get(machine)}" if os_key and machine in arch else None
    # Only the platforms a release publishes.
    return key if key in {"win32-x64", "darwin-arm64", "darwin-x64"} else None


def load_pins(path: Path = PINS_PATH) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def exe_name(system: str | None = None) -> str:
    return "tesseract.exe" if (system or sys.platform) == "win32" else "tesseract"


def install_dir(cache_dir: Path, tag: str) -> Path:
    return Path(cache_dir) / "tesseract" / tag


def installed_binary(
    cache_dir: Path | None = None,
    pins: dict[str, Any] | None = None,
    system: str | None = None,
) -> str | None:
    """Path of the unpacked portable Tesseract, or None when absent."""
    cache_dir = _cache_dir if cache_dir is None else cache_dir
    if cache_dir is None:
        return None
    try:
        pins = load_pins() if pins is None else pins
    except (OSError, ValueError):
        return None
    root = install_dir(cache_dir, pins["tag"])
    exe = root / exe_name(system)
    if exe.is_file() and (root / "tessdata" / "eng.traineddata").is_file():
        return str(exe)
    return None


def download(
    url: str,
    dest: Path,
    *,
    expected_sha: str,
    max_bytes: int,
    timeout: float = TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    """Stream url to dest, hashing as it goes. urllib follows redirects
    (GitHub serves release assets from a CDN host)."""
    digest = hashlib.sha256()
    total = 0
    with opener(url, timeout=timeout) as resp, open(dest, "wb") as out:
        while chunk := resp.read(1 << 16):
            total += len(chunk)
            if total > max_bytes:
                raise IntegrityError(
                    f"download is larger than the pinned {max_bytes} bytes"
                )
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != expected_sha:
        raise IntegrityError(
            f"sha256 {digest.hexdigest()} does not match the pinned {expected_sha}"
        )


def safe_extract(zip_path: Path, dest: Path, system: str | None = None) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            parts = PurePosixPath(name.replace("\\", "/")).parts
            if (
                name.startswith(("/", "\\"))
                or ".." in parts
                or (parts and ":" in parts[0])
            ):
                raise IntegrityError(f"unsafe path in zip: {name!r}")
        zf.extractall(dest)
    exe = dest / exe_name(system)
    if (system or sys.platform) != "win32" and exe.exists():
        exe.chmod(0o755)


def install(
    cache_dir: Path,
    pins: dict[str, Any],
    key: str,
    *,
    base: str = DOWNLOAD_BASE,
    system: str | None = None,
    fetch: Callable[..., None] = download,
) -> str:
    """Download, verify, unpack; returns the binary path. Atomic: the final
    folder appears only complete, and a concurrent winner's copy is kept."""
    asset = pins["assets"][key]
    final = install_dir(cache_dir, pins["tag"])
    final.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".work-", dir=final.parent))
    try:
        archive = work / asset["file"]
        fetch(
            f"{base}/{pins['tag']}/{asset['file']}",
            archive,
            expected_sha=asset["sha256"],
            # The pinned size is exact; the 10 % cap only stops a runaway
            # stream before the hash check would reject it anyway.
            max_bytes=int(asset["size"] * 1.1) + 1024,
        )
        unpacked = work / "x"
        unpacked.mkdir()
        safe_extract(archive, unpacked, system)
        try:
            os.rename(unpacked, final)
        except OSError:
            if not (final / exe_name(system)).is_file():
                raise  # not a lost race: a real failure
        return str(final / exe_name(system))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _worker(cache_dir: Path, pins: dict[str, Any], key: str, **kw: Any) -> None:
    global _failed, _last_error  # noqa: PLW0603
    try:
        install(cache_dir, pins, key, **kw)
        _last_error = None
    except IntegrityError as exc:
        _failed = str(exc)
        logger.warning("portable Tesseract rejected: %s", exc)
    except Exception as exc:  # noqa: BLE001 - offline etc.: retry next call
        _last_error = str(exc)
        logger.info("portable Tesseract download failed: %s", exc)


def ensure(
    wait: float = WAIT_SECONDS,
    *,
    cache_dir: Path | None = None,
    pins: dict[str, Any] | None = None,
    key: str | None = "auto",
    base: str = DOWNLOAD_BASE,
    system: str | None = None,
) -> tuple[str, str | None]:
    """("ready", exe) | ("downloading", None) | ("unavailable", reason)."""
    global _thread  # noqa: PLW0603
    cache_dir = _cache_dir if cache_dir is None else cache_dir
    if cache_dir is None:
        return "unavailable", "no cache directory configured"
    if pins is None:
        try:
            pins = load_pins()
        except (OSError, ValueError) as exc:
            return "unavailable", f"no Tesseract pins shipped: {exc}"
    key = platform_key() if key == "auto" else key
    exe = installed_binary(cache_dir, pins, system)
    if exe:
        return "ready", exe
    if key is None or key not in pins.get("assets", {}):
        return "unavailable", "no portable Tesseract is published for this computer"
    if _failed:
        return "unavailable", f"the downloaded Tesseract failed its check: {_failed}"
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(
                target=_worker,
                args=(cache_dir, pins, key),
                kwargs={"base": base, "system": system},
                name="pdf-mcp-tesseract-download",
                daemon=True,
            )
            _thread.start()
        thread = _thread
    thread.join(timeout=wait)
    exe = installed_binary(cache_dir, pins, system)
    if exe:
        return "ready", exe
    if thread.is_alive():
        return "downloading", None
    if _failed:
        return "unavailable", f"the downloaded Tesseract failed its check: {_failed}"
    return "unavailable", _last_error or "the download did not complete"
