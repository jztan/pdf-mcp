"""Tests for the Claude Desktop bundle builder (scripts/build_mcpb.py)."""

import asyncio
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")  # stdlib from 3.11; CI also runs 3.10

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import build_mcpb  # noqa: E402

ROOT = Path(__file__).parent.parent


def test_project_version_matches_package():
    from pdf_mcp import __version__

    assert build_mcpb.project_version(ROOT) == __version__


def test_manifest_version_is_stamped():
    files = build_mcpb.render_files("9.8.7", pins=[])
    manifest = json.loads(files["manifest.json"])
    assert manifest["version"] == "9.8.7"


def test_pyproject_pins_exact_version_and_caps_python():
    files = build_mcpb.render_files("9.8.7", pins=[])
    data = tomllib.loads(files["pyproject.toml"].decode())
    assert data["project"]["dependencies"] == ["pdf-mcp==9.8.7"]
    assert data["project"]["requires-python"] == ">=3.10,<3.14"
    assert "build-system" not in data


def test_manifest_runs_node_launcher_with_update_env():
    manifest = json.loads(build_mcpb.render_files("1.0.0", pins=[])["manifest.json"])
    server = manifest["server"]
    assert server["type"] == "node"
    assert server["entry_point"] == "server/launcher.js"
    assert server["mcp_config"]["command"] == "node"
    assert server["mcp_config"]["args"] == ["${__dirname}/server/launcher.js"]
    assert server["mcp_config"]["env"] == {
        "PDF_MCP_UPDATE_CHECK": "${user_config.update_check}"
    }
    assert manifest["user_config"]["update_check"]["default"] is True
    assert manifest["compatibility"]["runtimes"] == {"node": ">=16.0.0"}
    assert "python" not in manifest["compatibility"]["runtimes"]


def test_dependencies_are_the_exported_pins_plus_exact_pdf_mcp():
    pins = ["anyio==4.13.0", "colorama==0.4.6 ; sys_platform == 'win32'"]
    data = tomllib.loads(
        build_mcpb.render_files("9.8.7", pins=pins)["pyproject.toml"].decode()
    )
    assert data["project"]["dependencies"] == ["pdf-mcp==9.8.7", *pins]


def test_export_pins_reads_uv_export(tmp_path):
    class Result:
        stdout = (
            "# header\nanyio==4.13.0\n    # via fastmcp\n"
            "colorama==0.4.6 ; sys_platform == 'win32'\n"
        )

    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return Result()

    assert build_mcpb.export_pins(tmp_path, run=run) == [
        "anyio==4.13.0",
        "colorama==0.4.6 ; sys_platform == 'win32'",
    ]
    assert calls[0][:2] == ["uv", "export"]
    assert "--frozen" in calls[0] and "--no-emit-project" in calls[0]


def test_real_export_includes_core_dependencies():
    """Against this repo's uv.lock: the tested set, not install-day latest."""
    pins = build_mcpb.export_pins()
    names = {p.split("==")[0] for p in pins}
    assert {"fastmcp", "fastembed", "pypdfium2"} <= names
    assert not any(p.startswith("pdf-mcp==") for p in pins)


def test_manifest_tools_match_registered_tools():
    """Adding a tool without adding it to the manifest fails here."""
    from pdf_mcp.server import mcp

    registered = {t.name for t in asyncio.run(mcp.list_tools())}
    manifest = json.loads(build_mcpb.render_files("1.0.0", pins=[])["manifest.json"])
    assert {t["name"] for t in manifest["tools"]} == registered


def test_bundle_contains_exactly_the_expected_entries(tmp_path):
    path, _ = build_mcpb.build("1.0.0", tmp_path, pins=[])
    assert path.name == "pdf-mcp-1.0.0.mcpb"
    with zipfile.ZipFile(path) as zf:
        assert sorted(zf.namelist()) == [
            ".mcpbignore",
            "icon.png",
            "manifest.json",
            "pyproject.toml",
            "server/launcher.js",
            "server/uv-pins.json",
            "src/server.py",
        ]


def test_build_is_byte_reproducible(tmp_path):
    """server.json carries the hash computed at bump time; the uploaded
    file must hash the same."""
    _, sha_a = build_mcpb.build("1.0.0", tmp_path / "a", pins=[])
    _, sha_b = build_mcpb.build("1.0.0", tmp_path / "b", pins=[])
    assert sha_a == sha_b


def test_wheel_override_adds_uv_source(tmp_path):
    wheel = tmp_path / "pdf_mcp-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    data = tomllib.loads(
        build_mcpb.render_files("1.0.0", wheel=wheel, pins=[])[
            "pyproject.toml"
        ].decode()
    )
    assert data["tool"]["uv"]["sources"]["pdf-mcp"] == {
        "path": wheel.resolve().as_posix()
    }


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx not installed")
def test_manifest_passes_official_validator(tmp_path):
    files = build_mcpb.render_files("1.0.0", pins=[])
    (tmp_path / "manifest.json").write_bytes(files["manifest.json"])
    (tmp_path / "icon.png").write_bytes(files["icon.png"])
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "launcher.js").write_bytes(files["server/launcher.js"])
    result = subprocess.run(
        ["npx", "-y", "@anthropic-ai/mcpb@2.1.2", "validate", "manifest.json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        shell=sys.platform == "win32",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_launcher_carries_the_pinned_sdk_protocol_versions():
    """Early init answers `initialize` before the Python server exists; its
    version rule must use the same list the real server will."""
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    from mcp.types import LATEST_PROTOCOL_VERSION

    js = build_mcpb.render_files("1.0.0", pins=[])["server/launcher.js"].decode()
    quoted = ", ".join(f"'{v}'" for v in SUPPORTED_PROTOCOL_VERSIONS)
    assert f"const SUPPORTED_PROTOCOL_VERSIONS = [{quoted}];" in js
    assert f"const LATEST_PROTOCOL_VERSION = '{LATEST_PROTOCOL_VERSION}';" in js


def test_render_launcher_rewrites_both_lines():
    src = (
        "const SUPPORTED_PROTOCOL_VERSIONS = ['a'];\n"
        "const LATEST_PROTOCOL_VERSION = 'a';\n"
    )
    out = build_mcpb.render_launcher(src, ["x", "y"], "y")
    assert "const SUPPORTED_PROTOCOL_VERSIONS = ['x', 'y'];" in out
    assert "const LATEST_PROTOCOL_VERSION = 'y';" in out
    with pytest.raises(SystemExit):
        build_mcpb.render_launcher("nothing here", ["x"], "x")
