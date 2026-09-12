"""Regenerate packaging/mcpb/server/uv-pins.json for a uv release.

Run by hand when bumping uv, never at release time:

    python scripts/update_uv_pins.py 0.12.13
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
PINS_PATH = ROOT / "packaging" / "mcpb" / "server" / "uv-pins.json"
BASE = "https://github.com/astral-sh/uv/releases/download"
TARGETS = {
    "win32-x64": "uv-x86_64-pc-windows-msvc.zip",
    "win32-arm64": "uv-aarch64-pc-windows-msvc.zip",
    "darwin-x64": "uv-x86_64-apple-darwin.tar.gz",
    "darwin-arm64": "uv-aarch64-apple-darwin.tar.gz",
    "linux-x64": "uv-x86_64-unknown-linux-gnu.tar.gz",
    "linux-arm64": "uv-aarch64-unknown-linux-gnu.tar.gz",
}


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        return resp.read().decode("utf-8")


def build_pins(version: str, fetch: Callable[[str], str] = _fetch) -> dict:
    assets = {}
    for key, file in TARGETS.items():
        sha = fetch(f"{BASE}/{version}/{file}.sha256").split()[0].lower()
        if len(sha) != 64:
            raise SystemExit(f"bad sha256 for {file}: {sha!r}")
        assets[key] = {"file": file, "sha256": sha}
    return {"version": version, "assets": assets}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: update_uv_pins.py <uv-version>")
    pins = build_pins(args[0])
    PINS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PINS_PATH.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {PINS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
