"""Import-graph rules for pdf_mcp._core, pdf_mcp.server and pdf_mcp.tools.

Direction is one-way: server -> tool modules -> _helper modules -> _core.
Shared state lives in _core and is read as `_core.cache`, never bound.
"""

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "src" / "pdf_mcp"
STATE_NAMES = {
    "cache",
    "url_fetcher",
    "pdf_config",
    "_UPDATE_CHECK_ENABLED",
    "_OCR_AUTO_INSTALL",
    "_pending_notice",
    "RENDER_RESULT_BYTE_BUDGET",
}


def _modules() -> dict[str, Path]:
    mods = {"_core": PKG / "_core.py", "server": PKG / "server.py"}
    for path in sorted((PKG / "tools").glob("*.py")):
        if path.name != "__init__.py":
            mods[f"tools.{path.stem}"] = path
    return mods


def _layer(name: str) -> int:
    if name == "server":
        return 0
    if name == "_core":
        return 3
    return 2 if name.split(".")[1].startswith("_") else 1


def _edges(name: str, path: Path) -> set[str]:
    """Layout modules that `name` imports (relative imports only)."""
    known = set(_modules())
    base = ["tools"] if name.startswith("tools.") else []
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        parts = base[: len(base) - (node.level - 1)]
        target = ".".join(parts + (node.module.split(".") if node.module else []))
        candidates = [target] + [
            ".".join(filter(None, [target, a.name])) for a in node.names
        ]
        found.update(c for c in candidates if c in known and c != name)
    return found


def test_layout_modules_exist():
    assert {"_core", "server", "tools.search", "tools._render"} <= set(_modules())


def test_imports_only_point_down_the_layers():
    bad = []
    for name, path in _modules().items():
        for dep in _edges(name, path):
            same_helper_layer = _layer(name) == 2 and _layer(dep) == 2
            if _layer(dep) <= _layer(name) and not same_helper_layer:
                bad.append(f"{name} -> {dep}")
    assert not bad, f"upward or sideways imports: {bad}"


def test_no_import_cycles():
    graph = {n: _edges(n, p) for n, p in _modules().items()}
    state: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> None:
        if state.get(node) == 1:
            raise AssertionError(f"import cycle: {' -> '.join(trail + [node])}")
        if state.get(node) == 2:
            return
        state[node] = 1
        for dep in sorted(graph[node]):
            visit(dep, trail + [node])
        state[node] = 2

    for name in sorted(graph):
        visit(name, [])


def test_state_is_never_bound_outside_core():
    bad = []
    for name, path in _modules().items():
        if name == "_core":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "_core"
            ):
                bad += [
                    f"{name}: from _core import {a.name}"
                    for a in node.names
                    if a.name in STATE_NAMES
                ]
        for node in tree.body:
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target] if isinstance(node, ast.AnnAssign) else []
            )
            bad += [
                f"{name}: module-level {t.id} = ..."
                for t in targets
                if isinstance(t, ast.Name) and t.id in STATE_NAMES
            ]
    assert not bad, f"read state as _core.<name> instead: {bad}"
