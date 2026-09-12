"""Regenerate src/pdf_mcp/tesseract_pins.json from a pdf-mcp-tesseract release.

Run by hand when adopting a new Tesseract release, never at pdf-mcp release
time:

    python scripts/update_tesseract_pins.py tesseract-5.5.2-pdfmcp1
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
PINS_PATH = ROOT / "src" / "pdf_mcp" / "tesseract_pins.json"
REPO = "jztan/pdf-mcp-tesseract"
#: release asset suffix -> pdf-mcp platform key (portable_tesseract.platform_key)
PLATFORMS = {
    "win-x64": "win32-x64",
    "macos-arm64": "darwin-arm64",
    "macos-x64": "darwin-x64",
}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return bytes(resp.read())


def build_pins(tag: str, get: Callable[[str], bytes] = _get) -> dict[str, Any]:
    release = json.loads(
        get(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")
    )
    assets = {a["name"]: a for a in release["assets"]}
    pins: dict[str, Any] = {"tag": tag, "assets": {}}
    for suffix, key in PLATFORMS.items():
        name = f"pdf-mcp-tesseract-{suffix}.zip"
        if name not in assets or f"{name}.sha256" not in assets:
            raise SystemExit(f"{tag} is missing {name} or its .sha256")
        sha = get(assets[f"{name}.sha256"]["browser_download_url"]).decode().split()[0]
        if len(sha) != 64:
            raise SystemExit(f"bad sha256 for {name}: {sha!r}")
        pins["assets"][key] = {
            "file": name,
            "size": assets[name]["size"],
            "sha256": sha.lower(),
        }
    return pins


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: update_tesseract_pins.py <release tag>")
    pins = build_pins(args[0])
    PINS_PATH.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {PINS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
