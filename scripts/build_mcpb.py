"""Build the Claude Desktop bundle (.mcpb) for a pdf-mcp release.

The bundle is a node-type MCP Bundle: a manifest, a Node launcher that
fetches a pinned uv, a one-line Python shim, and a pyproject that pins the
released pdf-mcp wheel plus every dependency at the uv.lock versions. Output is
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
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "packaging" / "mcpb"
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)
_EXPORT_CMD = [
    "uv",
    "export",
    "--frozen",
    "--no-dev",
    "--no-hashes",
    "--no-emit-project",
    "--format",
    "requirements-txt",
]


def project_version(root: Path = ROOT) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("version not found in pyproject.toml")
    return match.group(1)


def bundle_filename(version: str) -> str:
    return f"pdf-mcp-{version}.mcpb"


def export_pins(root: Path = ROOT, run=subprocess.run) -> list[str]:
    """Every runtime dependency at the version uv.lock resolved (what CI
    tested), with environment markers, so the bundle does not float to
    whatever is newest on install day."""
    result = run(_EXPORT_CMD, cwd=root, capture_output=True, text=True, check=True)
    pins = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pins.append(line)
    return pins


def protocol_versions() -> tuple[list[str], str]:
    """The MCP protocol versions of the pinned Python SDK (this build venv
    installs the same mcp pin as the bundle, both from uv.lock). The
    launcher answers `initialize` early with them, so its answer must match
    what the real server agrees to at hand-over."""
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    from mcp.types import LATEST_PROTOCOL_VERSION

    return list(SUPPORTED_PROTOCOL_VERSIONS), str(LATEST_PROTOCOL_VERSION)


def render_launcher(source: str, supported: list[str], latest: str) -> str:
    supported_line = (
        f"const SUPPORTED_PROTOCOL_VERSIONS = {json.dumps(supported)};".replace(
            '"', "'"
        ).replace("','", "', '")
    )
    latest_line = f"const LATEST_PROTOCOL_VERSION = '{latest}';"
    out, n1 = re.subn(
        r"^const SUPPORTED_PROTOCOL_VERSIONS = .*;$",
        supported_line,
        source,
        flags=re.M,
    )
    out, n2 = re.subn(
        r"^const LATEST_PROTOCOL_VERSION = .*;$", latest_line, out, flags=re.M
    )
    if n1 != 1 or n2 != 1:
        raise SystemExit("launcher.js protocol-version lines not found")
    return out


def render_files(
    version: str, wheel: Path | None = None, pins: list[str] | None = None
) -> dict[str, bytes]:
    pins = export_pins() if pins is None else pins
    manifest = json.loads((TEMPLATE / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = version
    deps = [f"pdf-mcp=={version}", *pins]
    dep_lines = ",\n".join(f"    {json.dumps(d)}" for d in deps)
    pyproject = (TEMPLATE / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = pyproject.replace("{dependencies}", dep_lines)
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
        "server/launcher.js": render_launcher(
            (TEMPLATE / "server" / "launcher.js").read_text(encoding="utf-8"),
            *protocol_versions(),
        ).encode("utf-8"),
        "server/uv-pins.json": (TEMPLATE / "server" / "uv-pins.json").read_bytes(),
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


def build(
    version: str,
    out_dir: Path,
    wheel: Path | None = None,
    pins: list[str] | None = None,
) -> tuple[Path, str]:
    out_path = out_dir / bundle_filename(version)
    sha = write_bundle(render_files(version, wheel, pins), out_path)
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
