"""Guard against constructs newer than the minimum supported Python.

`pyproject.toml` declares `requires-python = ">=3.10"` and CI runs 3.10
through 3.14, but development happens on one interpreter, so a 3.11+
construct passes every local check and only fails in CI. An unguarded
`import tomllib` (3.11+) did exactly that: because it fails at *collection*,
one bad import took the whole suite down with exit 2 rather than failing a
single test.

This checks statically, so it costs milliseconds and needs no second
interpreter. It cannot catch everything a real 3.10 run would (new syntax,
new methods on existing types); it catches the import-level case, which is
the one that kills collection.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Top-level stdlib modules by the Python version that introduced them.
# Only modules newer than any version this package still supports need an
# entry; extend when raising or when a newer stdlib module gets used.
STDLIB_ADDED_IN = {
    "tomllib": (3, 11),
    "annotationlib": (3, 14),
}

# scripts/archive holds retired spikes that no gate runs or ships.
SCANNED_DIRS = ("src", "tests", "scripts")
SKIPPED_PARTS = {"archive", ".venv", "__pycache__"}


def _min_python() -> tuple[int, int]:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'requires-python\s*=\s*"[^0-9]*([0-9]+)\.([0-9]+)', text)
    assert match, "could not read requires-python from pyproject.toml"
    return int(match.group(1)), int(match.group(2))


def _source_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        for path in (REPO_ROOT / directory).rglob("*.py"):
            if SKIPPED_PARTS & set(path.parts):
                continue
            files.append(path)
    return files


def _unguarded_top_level_modules(tree: ast.Module) -> set[tuple[str, int]]:
    """Modules imported without a version fallback, with their line numbers.

    Two forms count as guarded. The idiom this repo uses is a version test
    (`if sys.version_info >= (3, 11): import tomllib / else: import tomli as
    tomllib`, as in src/pdf_mcp/config.py); `try: import ... / except
    ImportError:` is accepted too, since it fails over the same way.
    """
    guarded: set[int] = set()
    for node in ast.walk(tree):
        version_gated = isinstance(node, ast.If) and any(
            isinstance(sub, ast.Attribute) and sub.attr == "version_info"
            for sub in ast.walk(node.test)
        )
        if isinstance(node, ast.Try) or version_gated:
            for child in ast.walk(node):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(child))

    found: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, never stdlib.
            if node.level == 0 and node.module:
                found.add((node.module.split(".")[0], node.lineno))
    return found


def test_no_stdlib_imports_newer_than_requires_python() -> None:
    """Importing a module that does not exist on the minimum supported
    Python must be guarded by a try/except, or the suite dies at collection
    on that interpreter."""
    minimum = _min_python()
    offenders: list[str] = []

    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, lineno in _unguarded_top_level_modules(tree):
            added = STDLIB_ADDED_IN.get(module)
            if added is not None and added > minimum:
                rel = path.relative_to(REPO_ROOT)
                offenders.append(
                    f"{rel}:{lineno}: `import {module}` needs Python "
                    f"{added[0]}.{added[1]}+, but this package supports "
                    f"{minimum[0]}.{minimum[1]}+. Gate it on a version "
                    f"check with a backport, as src/pdf_mcp/config.py does: "
                    f"`if sys.version_info >= ({added[0]}, {added[1]}): "
                    f"import {module} / else: import <backport> as {module}`."
                )

    assert not offenders, "\n  " + "\n  ".join(sorted(offenders))


def test_the_guard_actually_sees_an_unguarded_import(tmp_path: Path) -> None:
    """The check above passes trivially if the parser never finds anything,
    so pin that a bare import is detected and a guarded one is not."""
    bare = ast.parse("import tomllib\n")
    assert ("tomllib", 1) in _unguarded_top_level_modules(bare)

    version_gated = ast.parse(
        "import sys\n"
        "if sys.version_info >= (3, 11):\n"
        "    import tomllib\n"
        "else:\n"
        "    import tomli as tomllib\n"
    )
    modules = {name for name, _ in _unguarded_top_level_modules(version_gated)}
    assert "tomllib" not in modules
    assert "tomli" not in modules

    try_gated = ast.parse(
        "try:\n    import tomllib\nexcept ImportError:\n    import tomli\n"
    )
    modules = {name for name, _ in _unguarded_top_level_modules(try_gated)}
    assert "tomllib" not in modules


def test_known_backport_sites_stay_guarded() -> None:
    """The three real call sites use the fallback idiom. If one is
    'simplified' to a bare import, that is the regression this file exists
    to catch."""
    for rel in (
        "src/pdf_mcp/config.py",
        "tests/test_docker_contract.py",
        "tests/test_docs_consistency.py",
    ):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        modules = {name for name, _ in _unguarded_top_level_modules(tree)}
        assert "tomllib" not in modules, f"{rel} imports tomllib unguarded"
