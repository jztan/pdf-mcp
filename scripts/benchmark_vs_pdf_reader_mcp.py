#!/usr/bin/env python
"""
scripts/benchmark_vs_pdf_reader_mcp.py

End-to-end MCP benchmark: pdf-mcp vs. @sylphx/pdf-reader-mcp.

Spawns each server over stdio, performs the MCP initialize handshake, and times
real tool calls on the SAME PDF — measuring what an agent actually experiences
(MCP round trip + parse + extract), not just the internal extraction function.

It compares only the operations both servers support:

  - full_text : pdf-mcp `pdf_read_all`  vs  pdf-reader-mcp `read_pdf`
                (include_full_text=true) — their headline "parallel" claim
  - info      : pdf-mcp `pdf_info`       vs  pdf-reader-mcp `read_pdf`
                (include_metadata + include_page_count)

Fairness notes:
  - Each timed iteration reads from a UNIQUE temp copy of the PDF, so neither
    side benefits from path-keyed caching — this measures cold extraction for
    both. pdf-mcp's SQLite cache is then shown separately as a "warm" row
    (same path, second call) since that is a real pdf-mcp advantage in practice.
  - Cross-language (Node/PDF.js vs Python/PyMuPDF) and cross-design (one
    mega-tool vs specialized tools); read the numbers as directional.

Usage:
    python scripts/benchmark_vs_pdf_reader_mcp.py --pdf /path/to/file.pdf
    python scripts/benchmark_vs_pdf_reader_mcp.py --pdf F --runs 7 --output OUT.md
    python scripts/benchmark_vs_pdf_reader_mcp.py --pdf F \
        --ours-cmd "pdf-mcp" --theirs-cmd "npx -y @sylphx/pdf-reader-mcp"

Requires: pdf-mcp installed (this repo) and Node.js for the other server
(fetched on first run via npx, or point --theirs-cmd at an install).
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _mcp_client import MCPClient  # noqa: E402


def _resp_size(result: dict[str, Any]) -> int:
    """Rough output-size sanity metric: total chars across content blocks."""
    total = 0
    for block in result.get("content", []):
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            total += len(block["text"])
    return total


def time_call(
    client: MCPClient, name: str, args_fn: Any, runs: int
) -> tuple[float, float, int]:
    """Warmup once, then time `runs` calls. args_fn(i) yields the arguments so
    each call can target a fresh temp path. Returns (min_s, median_s, size)."""
    client.call_tool(name, args_fn(-1))  # warmup
    samples, size = [], 0
    for i in range(runs):
        args = args_fn(i)
        start = time.perf_counter()
        result = client.call_tool(name, args)
        samples.append(time.perf_counter() - start)
        size = _resp_size(result)
    return min(samples), statistics.median(samples), size


def fresh_copies(src: Path, n: int, tmp: Path) -> list[str]:
    """n unique copies of the PDF so path-keyed caches never hit (cold path)."""
    out = []
    for i in range(n + 1):  # +1 for warmup index -1
        dst = tmp / f"copy_{i}_{src.name}"
        if not dst.exists():
            shutil.copy(src, dst)
        out.append(str(dst))
    return out


def fmt_table(title: str, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"### {title}",
        "",
        "| server | mode | min (ms) | median (ms) | output chars |",
        "|--------|------|---------:|------------:|-------------:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['server']} | {r['mode']} | {r['min_ms']:.1f} "
            f"| {r['median_ms']:.1f} | {r['size']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True, help="PDF to benchmark")
    parser.add_argument("--runs", type=int, default=5, help="timed runs per op")
    parser.add_argument("--ours-cmd", default="pdf-mcp", help="pdf-mcp launch cmd")
    parser.add_argument(
        "--theirs-cmd",
        default="npx -y @sylphx/pdf-reader-mcp",
        help="pdf-reader-mcp launch cmd",
    )
    parser.add_argument("--output", type=Path, help="write markdown report to FILE")
    args = parser.parse_args()

    if not args.pdf.exists():
        sys.exit(f"PDF not found: {args.pdf}")

    ours_cmd = args.ours_cmd.split()
    theirs_cmd = args.theirs_cmd.split()

    tmp = Path(tempfile.mkdtemp(prefix="mcp_vs_"))
    cold = fresh_copies(args.pdf, args.runs, tmp)  # index -1..runs-1 -> +1
    same = str(args.pdf)  # stable path for the warm (cached) row

    ft_rows: list[dict[str, Any]] = []
    info_rows: list[dict[str, Any]] = []

    # ----- pdf-mcp (ours) -----
    ours = MCPClient(ours_cmd)
    try:
        ours.initialize()
        # full text, cold (fresh path each call)
        mn, md, sz = time_call(
            ours, "pdf_read_all",
            lambda i: {"path": cold[i + 1], "max_pages": 100000}, args.runs,
        )
        ft_rows.append(dict(server="pdf-mcp", mode="cold", min_ms=mn * 1e3,
                            median_ms=md * 1e3, size=sz))
        # full text, warm (same path -> SQLite cache hit)
        mn, md, sz = time_call(
            ours, "pdf_read_all",
            lambda i: {"path": same, "max_pages": 100000}, args.runs,
        )
        ft_rows.append(dict(server="pdf-mcp", mode="warm(cache)", min_ms=mn * 1e3,
                            median_ms=md * 1e3, size=sz))
        # info
        mn, md, sz = time_call(
            ours, "pdf_info",
            lambda i: {"path": cold[i + 1]}, args.runs,
        )
        info_rows.append(dict(server="pdf-mcp", mode="cold", min_ms=mn * 1e3,
                              median_ms=md * 1e3, size=sz))
    finally:
        ours.close()

    # ----- pdf-reader-mcp (theirs) -----
    theirs = MCPClient(theirs_cmd)
    try:
        theirs.initialize()
        mn, md, sz = time_call(
            theirs, "read_pdf",
            lambda i: {"sources": [{"path": cold[i + 1]}],
                       "include_full_text": True}, args.runs,
        )
        ft_rows.append(dict(server="pdf-reader-mcp", mode="cold", min_ms=mn * 1e3,
                            median_ms=md * 1e3, size=sz))
        mn, md, sz = time_call(
            theirs, "read_pdf",
            lambda i: {"sources": [{"path": cold[i + 1]}],
                       "include_metadata": True,
                       "include_page_count": True}, args.runs,
        )
        info_rows.append(dict(server="pdf-reader-mcp", mode="cold", min_ms=mn * 1e3,
                              median_ms=md * 1e3, size=sz))
    finally:
        theirs.close()

    pages = "?"
    try:
        import pymupdf
        d = pymupdf.open(str(args.pdf))
        pages = str(len(d))
        d.close()
    except Exception:
        pass

    header = [
        "# pdf-mcp vs @sylphx/pdf-reader-mcp — end-to-end MCP benchmark",
        "",
        f"- PDF: {args.pdf.name} ({pages} pages) | runs: {args.runs}",
        "- Measured: MCP tools/call round trip on the same PDF, warmup + best-of.",
        "- cold = fresh temp copy each call (no cache); warm = pdf-mcp SQLite hit.",
        "- Cross-language (PyMuPDF vs PDF.js); read as directional.",
        "",
    ]
    out = "\n".join(header)
    ft = fmt_table("Full-text extraction", ft_rows)
    info = fmt_table("Info (metadata + page count)", info_rows)
    print(out)
    print(ft)
    print(info)

    if args.output:
        args.output.write_text(out + "\n" + ft + "\n" + info)
        print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
