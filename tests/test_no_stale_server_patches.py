"""No test, script or example may patch a name where the patch cannot bite.

First rule: nothing may patch or assign a name pdf_mcp.server does not export.

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
    tree = ast.parse(path.read_text(encoding="utf-8"))
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
        # The dotted target may be positional or passed as `target=`.
        for arg in [*args[:1], *(kw.value for kw in node.keywords)]:
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.startswith(PREFIX)
                and arg.value[len(PREFIX) :].split(".")[0] not in EXPORTED
            ):
                found.append(f"{rel}:{node.lineno} targets {arg.value!r}")
    return found


SRC = REPO / "src" / "pdf_mcp"
CORE_PREFIX = "pdf_mcp._core."


def _early_bound() -> set[str]:
    """`_core` names that tool modules import by name and `_core` never reads.

    A name `_core` also reads internally stays patchable there (the patch
    bites for `_core`'s own callers), so only tool-only consumers are listed.
    """
    bound: set[str] = set()
    for path in [SRC / "server.py", *sorted((SRC / "tools").glob("*.py"))]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "_core"
            ):
                bound.update(a.name for a in node.names)
    core = ast.parse((SRC / "_core.py").read_text(encoding="utf-8"))
    read_in_core = {
        n.id
        for n in ast.walk(core)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    return bound - read_in_core


EARLY_BOUND = _early_bound()


def _core_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "pdf_mcp._core" and a.asname:
                    aliases.add(a.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "pdf_mcp":
            aliases.update(a.asname or a.name for a in node.names if a.name == "_core")
    return aliases


def _early_bound_sites(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _core_aliases(tree)
    rel = path.relative_to(REPO) if path.is_relative_to(REPO) else path

    def is_core(expr: ast.AST) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id in aliases
        return ast.unparse(expr) == "pdf_mcp._core"

    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found += [
                f"{rel}:{node.lineno} assigns _core.{t.attr}"
                for t in targets
                if isinstance(t, ast.Attribute)
                and is_core(t.value)
                and t.attr in EARLY_BOUND
            ]
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
            and is_core(args[0])
            and isinstance(args[1], ast.Constant)
            and args[1].value in EARLY_BOUND
        ):
            found.append(f"{rel}:{node.lineno} patches _core.{args[1].value}")
        for arg in [*args[:1], *(kw.value for kw in node.keywords)]:
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.startswith(CORE_PREFIX)
                and arg.value[len(CORE_PREFIX) :].split(".")[0] in EARLY_BOUND
            ):
                found.append(f"{rel}:{node.lineno} targets {arg.value!r}")
    return found


def _files() -> list[Path]:
    me = Path(__file__).resolve()
    return [
        p
        for root in ("tests", "scripts", "examples")
        for p in sorted((REPO / root).rglob("*.py"))
        if p.resolve() != me
    ]


def test_the_scan_covers_examples_too():
    roots = {p.relative_to(REPO).parts[0] for p in _files()}
    assert {"tests", "scripts", "examples"} <= roots


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
        "patch(target='pdf_mcp.server.cache')\n",
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


# Second rule: a `_core` helper that tool modules bind by name
# (`from .._core import _resolve_path`) cannot be replaced by patching `_core`:
# each tool module already holds its own reference. Patch the tool module.


@pytest.mark.parametrize(
    "source",
    [
        "from pdf_mcp import _core\nmonkeypatch.setattr(_core, '_resolve_path', f)\n",
        "import pdf_mcp._core as c\nc._resolve_path = f\n",
        "patch('pdf_mcp._core._resolve_path')\n",
        "from pdf_mcp import _core\npatch.object(_core, '_pdf_hash')\n",
    ],
)
def test_the_scan_catches_patches_on_early_bound_core_names(tmp_path, source):
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _early_bound_sites(probe)


@pytest.mark.parametrize(
    "source",
    [
        # state is read as _core.<name> at call time, so this patch bites
        "from pdf_mcp import _core\nmonkeypatch.setattr(_core, 'cache', c)\n",
        # read inside _core itself (by _ocr_unavailable), so this bites there
        "from pdf_mcp import _core\nmonkeypatch.setattr(_core, '_lang_available', f)\n",
        "patch('pdf_mcp._core.check_tesseract_available')\n",
    ],
)
def test_the_scan_allows_core_patches_that_bite(tmp_path, source):
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _early_bound_sites(probe) == []


def test_early_bound_names_are_what_we_think():
    assert {"_resolve_path", "_clamp", "_pdf_hash"} <= EARLY_BOUND
    assert not {"cache", "pdf_config", "_lang_available"} & EARLY_BOUND


def test_no_patches_on_early_bound_core_names():
    sites = [site for path in _files() for site in _early_bound_sites(path)]
    assert not sites, (
        "these patch pdf_mcp._core, but tool modules bound the name at import;"
        " patch the pdf_mcp.tools.* module that uses it:\n" + "\n".join(sites)
    )
