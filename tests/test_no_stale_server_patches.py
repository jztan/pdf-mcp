"""No test or script may patch or assign a name pdf_mcp.server does not export.

`server_module.cache = ...` on the slim server module would silently create a
new attribute, and a benchmark would then run against the real user cache.
State and helpers live in pdf_mcp._core and pdf_mcp.tools.*.
"""

import ast
from pathlib import Path

import pytest

import pdf_mcp.server as server

REPO = Path(__file__).resolve().parents[1]
EXPORTED = set(server.__all__)
PATCH_CALLS = {"setattr", "delattr", "object"}  # monkeypatch.setattr, patch.object
PREFIX = "pdf_mcp.server."


def _server_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "pdf_mcp.server" and a.asname:
                    aliases.add(a.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "pdf_mcp":
            aliases.update(a.asname or a.name for a in node.names if a.name == "server")
    return aliases


def _is_server(expr: ast.AST, aliases: set[str]) -> bool:
    if isinstance(expr, ast.Name):
        return expr.id in aliases
    return ast.unparse(expr) == "pdf_mcp.server"


def _stale_sites(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    aliases = _server_aliases(tree)
    rel = path.relative_to(REPO) if path.is_relative_to(REPO) else path
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if (
                    isinstance(t, ast.Attribute)
                    and _is_server(t.value, aliases)
                    and t.attr not in EXPORTED
                ):
                    found.append(f"{rel}:{node.lineno} assigns server.{t.attr}")
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        call_name = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        )
        args = node.args
        if (
            call_name in PATCH_CALLS
            and len(args) >= 2
            and _is_server(args[0], aliases)
            and isinstance(args[1], ast.Constant)
            and args[1].value not in EXPORTED
        ):
            found.append(f"{rel}:{node.lineno} patches server.{args[1].value}")
        for arg in args[:1]:
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.startswith(PREFIX)
                and arg.value[len(PREFIX) :].split(".")[0] not in EXPORTED
            ):
                found.append(f"{rel}:{node.lineno} targets {arg.value!r}")
    return found


def _files() -> list[Path]:
    me = Path(__file__).resolve()
    return [
        p
        for root in ("tests", "scripts")
        for p in sorted((REPO / root).rglob("*.py"))
        if p.resolve() != me
    ]


def test_server_exports_only_the_public_surface():
    assert len(EXPORTED) == 16  # 13 tools + mcp, main, main_http
    assert not {"cache", "pdf_config", "url_fetcher", "_resolve_path"} & set(
        vars(server)
    )


def test_no_stale_server_patch_targets():
    stale = [site for path in _files() for site in _stale_sites(path)]
    assert not stale, "patch pdf_mcp._core or pdf_mcp.tools.* instead:\n" + "\n".join(
        stale
    )


@pytest.mark.parametrize(
    "source",
    [
        "from pdf_mcp import server as s\ns.cache = 1\n",
        "import pdf_mcp.server\npdf_mcp.server.pdf_config = 1\n",
        "from pdf_mcp import server\nmonkeypatch.setattr(server, 'cache', 1)\n",
        "patch('pdf_mcp.server.url_fetcher')\n",
        "monkeypatch.setattr('pdf_mcp.server.cache', 1)\n",
    ],
)
def test_the_scan_catches_each_stale_form(tmp_path, source):
    probe = tmp_path / "probe.py"
    probe.write_text(source)
    assert _stale_sites(probe)


def test_the_scan_allows_tool_function_patches(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from pdf_mcp import server\n"
        "monkeypatch.setattr(server, 'pdf_corpus_warm', None)\n"
    )
    assert _stale_sites(probe) == []
