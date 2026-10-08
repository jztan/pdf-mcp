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

import hashlib
import platform
import sqlite3
import sys
from contextlib import closing
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable

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


# Corpus keyword and hybrid search run one BM25 query on the cache's shared
# FTS table, filtered to the searched files; FTS5 takes word rarity (IDF)
# from the whole table. So the documents a cache holds besides the corpus
# move corpus scores: 84 unrelated filings in a shared cache moved the
# Bedrock anchor's paraphrase class from 0.325 to 0.289 on identical code,
# and it read as a regression. Harnesses that score corpus search on the
# active cache gate on the index before scoring and record what it held.


def keyword_index_docs(db_path: str | Path) -> set[str]:
    """Distinct file paths in the cache's shared keyword index, read-only."""
    uri = f"file:{Path(db_path)}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute("SELECT DISTINCT file_path FROM pdf_search_fts")
        return {r[0] for r in rows}


def mixed_cache_gate(
    db_path: str | Path, searched: Iterable[str], allow: bool
) -> tuple[dict[str, Any], str | None]:
    """What the keyword index holds against the searched corpus, and an
    error unless the index holds only searched documents or `allow` is set.

    The composition goes into the results either way, so two runs can be
    checked for the same cache contents before their scores are compared."""
    indexed = keyword_index_docs(db_path)
    wanted = {str(Path(p).resolve()) for p in searched}
    extra = sorted(p for p in indexed if str(Path(p).resolve()) not in wanted)
    comp: dict[str, Any] = {
        "indexed_docs": len(indexed),
        "searched_docs": len(wanted),
        "extra_docs": len(extra),
        "extra_sample": [Path(p).name for p in extra[:5]],
        "digest": hashlib.sha256("\n".join(sorted(indexed)).encode()).hexdigest()[:16],
        "mixed_allowed": bool(extra) and allow,
    }
    if not extra or allow:
        return comp, None
    return comp, (
        f"the cache's keyword index holds {len(extra)} documents outside the"
        f" searched corpus (e.g. {', '.join(comp['extra_sample'])}). Corpus"
        " keyword and hybrid scores take word rarity from every indexed"
        " document, so this run would not be comparable with a corpus-only"
        " one. Score on a cache that holds only the corpus (PDF_MCP_CACHE_DIR"
        " where the harness reads it), or pass --allow-mixed-cache to score"
        " anyway (recorded in the output)."
    )
