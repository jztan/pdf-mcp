"""Build the Claude Desktop bundle (.mcpb) for a pdf-mcp release.

The bundle is a thin uv-type MCP Bundle: a manifest, a one-line shim and a
pyproject that pins the released pdf-mcp wheel from PyPI. Output is
byte-reproducible (sorted entries, fixed timestamps, fixed permissions), so
the SHA-256 that release.py writes into server.json at version-bump time
matches the file it uploads after PyPI confirms.

    python scripts/build_mcpb.py                 # dist/pdf-mcp-<ver>.mcpb
    python scripts/build_mcpb.py --wheel dist/pdf_mcp-*.whl --out smoke
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "packaging" / "mcpb"
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def project_version(root: Path = ROOT) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("version not found in pyproject.toml")
    return match.group(1)


def bundle_filename(version: str) -> str:
    return f"pdf-mcp-{version}.mcpb"


def render_files(version: str, wheel: Path | None = None) -> dict[str, bytes]:
    manifest = json.loads((TEMPLATE / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = version
    pyproject = (TEMPLATE / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = pyproject.replace("{version}", version)
    if wheel is not None:
        # Smoke tests only: install the locally built wheel instead of PyPI.
        pyproject += (
            "\n[tool.uv.sources]\n"
            f'pdf-mcp = {{ path = "{wheel.resolve().as_posix()}" }}\n'
        )
    return {
        "manifest.json": (json.dumps(manifest, indent=2) + "\n").encode("utf-8"),
        "pyproject.toml": pyproject.encode("utf-8"),
        "src/server.py": (TEMPLATE / "src" / "server.py").read_bytes(),
        "icon.png": (TEMPLATE / "icon.png").read_bytes(),
        ".mcpbignore": (TEMPLATE / ".mcpbignore").read_bytes(),
    }


def write_bundle(files: dict[str, bytes], out_path: Path) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=_FIXED_DATE)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[name])
    return hashlib.sha256(out_path.read_bytes()).hexdigest()


def build(version: str, out_dir: Path, wheel: Path | None = None) -> tuple[Path, str]:
    out_path = out_dir / bundle_filename(version)
    sha = write_bundle(render_files(version, wheel), out_path)
    return out_path, sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--version", default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--wheel", type=Path, default=None)
    args = parser.parse_args(argv)
    version = args.version or project_version()
    path, sha = build(version, args.out, args.wheel)
    print(f"{path} {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
