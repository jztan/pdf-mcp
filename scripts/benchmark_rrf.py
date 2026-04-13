#!/usr/bin/env python
"""
scripts/benchmark_rrf.py

Benchmark: RRF hybrid search vs keyword-only vs semantic-only.

Run synthetic scenarios (always):
    python scripts/benchmark_rrf.py

Run with a real PDF (optional — appends a "Real PDF" section):
    python scripts/benchmark_rrf.py --pdf path/to/doc.pdf \\
        --query "your query" --relevant-pages "1,3,5"

--pdf accepts a local path or a URL.
Always exits 0 (informational report, no CI gate).
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pymupdf  # noqa: E402
import pdf_mcp.server as server_module  # noqa: E402
from pdf_mcp.cache import PDFCache  # noqa: E402
from pdf_mcp.server import _resolve_path, pdf_search  # noqa: E402

# Detect fastembed once at import time.
try:
    import fastembed  # type: ignore  # noqa: F401
    _FASTEMBED_AVAILABLE = True
except ImportError:
    _FASTEMBED_AVAILABLE = False

# Accumulated output lines (with ANSI) for saving to files.
_OUTPUT: list[str] = []

_IS_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def bold(t: str) -> str:
    return _c("1", t)


def cyan(t: str) -> str:
    return _c("36", t)


def yellow(t: str) -> str:
    return _c("33", t)


def _p(text: str = "") -> None:
    """Print a line and append to output buffer for file saving."""
    _OUTPUT.append(text)
    print(text)


def _section(title: str) -> None:
    width = 68
    _p()
    _p(bold(cyan("=" * width)))
    _p(bold(cyan(f"  {title}")))
    _p(bold(cyan("=" * width)))


def _row(label: str, value: str, ok: bool | None = None) -> None:
    marker = ""
    if ok is True:
        marker = green(" ✓")
    elif ok is False:
        marker = red(" ✗")
    _p(f"  {label:<36} {value}{marker}")


def main() -> None:
    _p(bold("\npdf-mcp RRF Hybrid Search — Benchmark Report"))
    _p("─" * 68)
    sys.exit(0)


if __name__ == "__main__":
    main()
