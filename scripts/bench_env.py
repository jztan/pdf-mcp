"""The environment a benchmark ran in, and whether two runs can be timed
against each other.

Identical code measured ~37% slower on corpus semantic search from a
worktree whose venv had resolved Python 3.12 / SQLite 3.50 instead of the
main checkout's 3.13 / 3.51 (2026-09-12). It read as a regression until the
interpreters were swapped, because nothing in the output said which
interpreter produced it. Every benchmark that reports a timing records
`environment()` beside it, and a timing comparison between runs goes
through `timing_mismatch()` first.

Quality metrics (containment, NDCG, span recall) do not depend on these;
only wall-clock numbers do.
"""

from __future__ import annotations

import platform
import sqlite3
import sys
from importlib import metadata
from typing import Any

# The packages whose versions move a pdf-mcp timing: the engine, the
# encoder stack, and the numeric layer under both.
PACKAGES = ("pdf-mcp", "fastembed", "onnxruntime", "numpy", "pypdfium2")

# What must match for two timings to be comparable. The venv path is
# deliberately absent: two worktrees on the same interpreter and libraries
# time alike.
_TIMING_KEYS = ("implementation", "python", "sqlite", "machine")


def environment() -> dict[str, Any]:
    """Interpreter, SQLite, hardware and key package versions, as plain
    JSON-serialisable values. A package that is not installed reads
    "absent" rather than raising, so a partial install still reports."""
    packages: dict[str, str] = {}
    for name in PACKAGES:
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = "absent"
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "sqlite": sqlite3.sqlite_version,
        "machine": platform.machine(),
        "platform": platform.platform(),
        "executable": sys.executable,
        "packages": packages,
    }


def timing_mismatch(
    baseline: dict[str, Any] | None, current: dict[str, Any] | None
) -> list[str]:
    """Human-readable differences that make two timings incomparable;
    empty when they can be compared. A run that recorded no environment
    is never comparable: its interpreter is unknown."""
    if baseline is None:
        return ["baseline recorded no environment"]
    if current is None:
        return ["current run recorded no environment"]
    diffs = [
        f"{key} {baseline.get(key)} != {current.get(key)}"
        for key in _TIMING_KEYS
        if baseline.get(key) != current.get(key)
    ]
    b_pkgs = baseline.get("packages") or {}
    c_pkgs = current.get("packages") or {}
    for name in sorted(set(b_pkgs) | set(c_pkgs)):
        if b_pkgs.get(name) != c_pkgs.get(name):
            diffs.append(f"{name} {b_pkgs.get(name)} != {c_pkgs.get(name)}")
    return diffs


def markdown_line(env: dict[str, Any]) -> str:
    """One line for a results file header."""
    pkgs = ", ".join(f"{k} {v}" for k, v in sorted(env["packages"].items()))
    return (
        f"Environment: {env['implementation']} Python {env['python']},"
        f" SQLite {env['sqlite']}, {env['machine']} ({env['platform']}); {pkgs}."
    )
