"""pdf-mcp-warm: offline full-corpus prewarm, no MCP client in the loop.

``pdf_corpus_warm`` (the MCP tool) is built to fit inside one client-side
tool call: it caps at ``corpus.CORPUS_MAX_FILES`` files and
``budget_seconds <= 300`` per call (server.py), so warming a folder that
does not fit either limit means re-issuing the same tool call by hand,
possibly many times, from inside a chat session -- and a query against an
only-partly-warm corpus just times out in the meantime.

This entry point is not an MCP tool call, so neither limit applies: no
client-side timeout to respect, no reason to cap file count. It walks the
whole folder, chunks it internally (purely to keep each
``corpus.warm_docs`` call's own bookkeeping the same shape the tool
produces -- the chunk size is not a hard ceiling here), and warms every
chunk to completion, writing to the exact on-disk cache
(``PDF_MCP_CACHE_DIR``, ``~/.config/pdf-mcp/config.toml``'s allow-list and
``[embedding].model``) the server reads. Run it once before a chat
session so ``pdf_search`` / ``pdf_corpus_search`` hit a warm cache instead
of timing out.

Warms text, embeddings, AND the section-granularity search index by
default -- all three of ``pdf_search``'s query paths, not just page-level
keyword/semantic search. The section index in particular is otherwise
built lazily, in full, on a document's first
``pdf_search(granularity="section")`` call regardless of how warm the
rest of the cache is, which can by itself exceed a timeout-bounded MCP
client's budget on a large document; ``--no-sections`` opts out if you
don't need it.

Already-cached docs are free (see ``corpus.warm_docs``), so re-running
after an interrupt (Ctrl-C, a crash, a machine reboot) resumes rather than
redoing work already committed.

Usage:
    pdf-mcp-warm /path/to/pdfs --recursive
    pdf-mcp-warm doc1.pdf doc2.pdf --no-embeddings
    pdf-mcp-warm /path/to/pdfs --model BAAI/bge-small-en-v1.5
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import corpus
from .cache import PDFCache
from .config import PDFConfig

# Mirrors server.py's _cache_dir_from_env/_ttl_hours_from_env exactly
# (same env vars, same defaults, same validation) without importing
# server.py -- that module also builds the FastMCP app and registers
# every tool at import time, real weight this standalone CLI has no use
# for. Kept in sync by hand; the pair is small and rarely changes.
_DEFAULT_CACHE_TTL_HOURS = 24
_MAX_CACHE_TTL_HOURS = 8760  # one year


def _cache_dir_from_env() -> "Path | None":
    raw = os.environ.get("PDF_MCP_CACHE_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _ttl_hours_from_env() -> int:
    raw = os.environ.get("PDF_MCP_CACHE_TTL")
    if raw is None or raw.strip() == "":
        return _DEFAULT_CACHE_TTL_HOURS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"PDF_MCP_CACHE_TTL must be an integer (got {raw!r})"
        ) from exc
    if value < 0 or value > _MAX_CACHE_TTL_HOURS:
        raise ValueError(
            f"PDF_MCP_CACHE_TTL must be in [0, {_MAX_CACHE_TTL_HOURS}] hours "
            f"(up to one year; got {value})"
        )
    return value


def _discover_pdfs(paths: list[str], recursive: bool) -> list[str]:
    """Expand directories/files into a sorted, deduped list of .pdf paths.

    Mirrors ``corpus.resolve_corpus``'s directory-mode glob (case-
    insensitive ``*.pdf``, sorted) but with no
    ``corpus.CORPUS_MAX_FILES`` cap -- that cap keeps one MCP tool call's
    response small; this is a local walk, not a tool call, and the result
    is warmed in chunks by ``main`` regardless of how many files it finds.
    A path that is neither a file nor a directory is reported and
    skipped, not fatal to the rest of the run.
    """
    found: set[str] = set()
    for entry in paths:
        p = Path(entry).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        p = p.resolve()
        if p.is_dir():
            walker = p.rglob("*") if recursive else p.iterdir()
            for f in walker:
                if f.is_file() and f.suffix.lower() == ".pdf":
                    found.add(str(f))
        elif p.is_file():
            found.add(str(p))
        else:
            print(f"skip (not found): {entry}", file=sys.stderr)
    return sorted(found)


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pdf-mcp-warm",
        description=(
            "Warm the pdf-mcp cache for a folder (or explicit files) to "
            "completion, with no MCP client-side timeout or file-count "
            "limit in the way. Run this once before a chat session so "
            "pdf_search / pdf_corpus_search hit a warm cache instead of "
            "timing out."
        ),
    )
    ap.add_argument("paths", nargs="+", help="PDF files and/or directories")
    ap.add_argument(
        "--recursive", action="store_true", help="recurse into subdirectories"
    )
    group = ap.add_mutually_exclusive_group()
    group.add_argument(
        "--embeddings",
        dest="embeddings",
        action="store_true",
        default=True,
        help=(
            "also warm embeddings (default: on -- a prewarm that skips "
            "vectors still leaves semantic search to time out at query time)"
        ),
    )
    group.add_argument(
        "--no-embeddings",
        dest="embeddings",
        action="store_false",
        help="text only, skip the embedding pass",
    )
    section_group = ap.add_mutually_exclusive_group()
    section_group.add_argument(
        "--sections",
        dest="sections",
        action="store_true",
        default=True,
        help=(
            "also build the section-granularity search index (default: on "
            "-- pdf_search(granularity='section') otherwise builds it lazily "
            "on first use, per doc, which can itself time out a large "
            "document even after a full prewarm)"
        ),
    )
    section_group.add_argument(
        "--no-sections",
        dest="sections",
        action="store_false",
        help="skip the section index",
    )
    ap.add_argument(
        "--model",
        default=None,
        help=(
            "embedding model name (default: [embedding].model in "
            "config.toml, or the built-in default)"
        ),
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=corpus.CORPUS_MAX_FILES,
        help=(
            f"files per warm_docs call (default: {corpus.CORPUS_MAX_FILES}, "
            "matching corpus.CORPUS_MAX_FILES; purely a progress-reporting "
            "grain here, not a cap on the total run)"
        ),
    )
    args = ap.parse_args(argv)

    pdf_config = PDFConfig()
    try:
        cache = PDFCache(
            cache_dir=_cache_dir_from_env(), ttl_hours=_ttl_hours_from_env()
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    model_name = args.model or pdf_config.embedding_model
    embed_fn = None
    if args.embeddings:
        from . import embedder

        try:
            embedder.check_available(model_name)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not raised
            print(f"error: {exc}", file=sys.stderr)
            print(
                "hint: install the embedding extra, fix the configured "
                "model, or pass --no-embeddings.",
                file=sys.stderr,
            )
            return 1

        def _embed(texts: list[str]) -> list[bytes]:
            vecs = embedder.encode(texts, model_name)
            return [v.tobytes() for v in vecs]

        embed_fn = _embed

    files = _discover_pdfs(args.paths, args.recursive)
    if not files:
        print("No PDF files found.", file=sys.stderr)
        return 1
    print(f"Found {len(files)} PDF file(s).", file=sys.stderr)

    total_warmed = total_skipped = total_unprocessed = 0
    t0 = time.monotonic()
    batch_size = max(1, args.batch_size)
    batches = _chunks(files, batch_size)
    for i, batch in enumerate(batches, 1):
        res = corpus.resolve_corpus(
            batch, recursive=False, check_path=pdf_config.check_path
        )
        if "error" in res:
            # Cannot happen at len(batch) <= CORPUS_MAX_FILES from an
            # explicit list (the only error case left is "no files",
            # already excluded by the batch being non-empty), but treat
            # it like any other unusable batch rather than assume.
            print(f"batch {i}/{len(batches)}: {res['error']}", file=sys.stderr)
            total_skipped += len(batch)
            continue
        for s in res["skipped"]:
            print(f"skip: {s['path']}: {s['reason']}", file=sys.stderr)
        total_skipped += len(res["skipped"])
        if not res["files"]:
            continue

        warm: dict[str, Any] = corpus.warm_docs(
            res["files"],
            budget_seconds=float("inf"),
            cache=cache,
            embeddings=args.embeddings,
            model_name=model_name,
            embed=embed_fn,
            sections=args.sections,
        )
        for s in warm["skipped"]:
            print(f"skip: {s['path']}: {s['reason']}", file=sys.stderr)
        total_skipped += len(warm["skipped"])
        total_unprocessed += len(warm["unprocessed"])
        total_warmed += warm["warmed_this_call"]
        elapsed = time.monotonic() - t0
        done = min(i * batch_size, len(files))
        print(
            f"[{done}/{len(files)}] batch {i}/{len(batches)}: "
            f"{warm['warmed_this_call']} warmed, {len(warm['docs'])} cache-verified, "
            f"{len(warm['unprocessed'])} unprocessed, {len(warm['skipped'])} skipped "
            f"({elapsed:.0f}s elapsed)",
            file=sys.stderr,
        )

    elapsed = time.monotonic() - t0
    print(
        f"\nDone in {elapsed:.0f}s: {total_warmed} warmed this run, "
        f"{total_unprocessed} unprocessed, {total_skipped} skipped, "
        f"{len(files)} total.",
        file=sys.stderr,
    )
    # skipped counts too, not just unprocessed: an unreadable file, a
    # path outside the allow-list, or a per-doc "warm failed" all land in
    # skipped rather than unprocessed, and this exit code is what a
    # pre-session gate (`pdf-mcp-warm ... && start_session`) checks.
    # Exiting 0 with half the corpus in skipped would make that gate lie.
    return 1 if (total_unprocessed or total_skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
