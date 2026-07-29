"""Corpus resolution and warm orchestration for multi-document tools.

Pure-logic module (no MCP dependency). ``resolve_corpus`` turns a
directory or explicit path list into a validated file list;
``warm_docs`` (Task 2) runs the budgeted warm loop that extracts text
(and optionally embeddings) into the existing per-doc cache. All
SQLite writes go through the ``PDFCache`` instance passed in by the
caller; this module owns no storage of its own.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import pymupdf

from .extractor import _warm_extract_worker

__all__ = [
    "CORPUS_MAX_FILES",
    "CORPUS_RRF_K",
    "resolve_corpus",
    "warm_docs",
    "text_coverage_label",
    "build_overview_card",
    "rrf_fuse_doc_rankings",
    "rrf_fuse_two_rankings",
    "rrf_fuse_two_rankings_scored",
]

# Hard ceiling on corpus size: the tens-of-docs design boundary made
# explicit. Beyond this, corpus tools return an inline error instead
# of silently truncating.
CORPUS_MAX_FILES = 100

# RRF constant for cross-document fusion; matches server._RRF_K so corpus
# fusion and single-doc hybrid fusion share one k. Design decided by the
# stage-2 ranking benchmark: per-document fusion, not corpus-wide FTS.
CORPUS_RRF_K = 60


def _validate_file(
    entry: str, check_path: Callable[[str], None] | None
) -> tuple[str, None] | tuple[None, str]:
    """Validate one corpus entry.

    Returns (resolved_path, None) on success or (None, reason).
    Mirrors the local branch of server._resolve_path: absolute-ise,
    resolve symlinks, extension check, config allow/deny, existence.
    URLs are rejected outright (corpus calls are local-only).
    """
    if "://" in entry:
        return None, (
            "URLs are not supported in corpus calls; fetch via a"
            " single-doc tool first"
        )
    path = Path(entry).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = path.resolve()
    if resolved.suffix.lower() != ".pdf":
        return None, f"not a .pdf file: {resolved.suffix or 'no extension'}"
    if check_path is not None:
        try:
            check_path(str(resolved))
        except ValueError as e:
            return None, str(e)
    if not resolved.exists():
        return None, "file not found"
    return str(resolved), None


def resolve_corpus(
    paths: str | list[str],
    recursive: bool = False,
    check_path: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Resolve a directory or path list into a validated corpus.

    Returns ``{"files": [...], "skipped": [{"path", "reason"}]}`` on
    success, or an inline ``{"error", "hint"}`` payload (missing
    directory, empty corpus, cap exceeded). Directory mode is
    non-recursive by default and matches ``*.pdf`` case-insensitively;
    results are sorted for determinism.
    """
    skipped: list[dict[str, str]] = []

    if isinstance(paths, str):
        if "://" in paths:
            return {
                "error": f"URLs are not accepted: {paths}",
                "hint": (
                    "Corpus tools operate on local files only. Use"
                    " single-doc tools (pdf_info, pdf_search) for URLs,"
                    " or download the file locally first."
                ),
            }
        root = Path(paths).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        root = root.resolve()
        if not root.is_dir():
            return {
                "error": f"Not a directory: {paths}",
                "hint": (
                    "Pass a directory containing PDFs, or an explicit"
                    " list of .pdf paths."
                ),
            }
        walker = root.rglob("*") if recursive else root.iterdir()
        candidates = sorted(
            str(p) for p in walker if p.is_file() and p.suffix.lower() == ".pdf"
        )
    else:
        candidates = list(paths)

    files: list[str] = []
    seen: set[str] = set()
    for entry in candidates:
        resolved, reason = _validate_file(entry, check_path)
        if resolved is None:
            skipped.append({"path": entry, "reason": reason or ""})
        elif resolved not in seen:
            seen.add(resolved)
            files.append(resolved)

    if not files:
        return {
            "error": "No PDF files found in corpus",
            "hint": (
                "Check the directory or paths; see `skipped` for" " per-file reasons."
            ),
            "skipped": skipped,
        }
    if len(files) > CORPUS_MAX_FILES:
        return {
            "error": (
                f"Corpus has {len(files)} PDFs, above the"
                f" {CORPUS_MAX_FILES}-file cap"
            ),
            "hint": (
                "Corpus tools target tens of documents. Narrow the"
                " directory or pass an explicit subset."
            ),
        }
    return {"files": files, "skipped": skipped}


def _cached_pages(
    path: str,
    cache: Any,
    embeddings: bool,
    model_name: str | None,
) -> int | None:
    """Return the doc's page count if it is fully warm in cache, else None.

    Fully warm = valid metadata row including a text_coverage map, all
    pages' text cached, and (when ``embeddings``) embeddings cached for
    every non-empty page. Staleness is handled by the cache layer's
    mtime checks: invalid rows simply come back missing.
    """
    meta = cache.get_metadata(path)
    if not meta or meta.get("text_coverage") is None:
        return None
    pages: int = meta["page_count"]
    texts = cache.get_pages_text(path, list(range(pages)))
    if len(texts) < pages:
        return None
    if embeddings:
        non_empty = [pn for pn, t in texts.items() if t.strip()]
        embs = cache.get_page_embeddings(path, non_empty, model_name)
        if len(embs) < len(non_empty):
            return None
    return pages


def _finalize_doc(
    path: str,
    page_count: int,
    metadata: dict[str, Any],
    toc: list[Any],
    texts: dict[int, str],
    coverage: list[dict[str, int]],
    cache: Any,
    embeddings: bool,
    model_name: str | None,
    embed: Callable[[list[str]], list[bytes]] | None,
) -> int:
    """Parent-side tail of warming one doc: OCR preservation, per-doc
    encode, then the three cache writes together (atomic per doc).

    Always runs in the parent process — every SQLite touch is here.
    """
    # Preserve previously-OCR'd pages: a scanned doc's page may already
    # carry non-empty OCR text (via pdf_read_pages(ocr=True)) even though
    # this doc was never "fully warm" (e.g. missing metadata/text_coverage
    # or missing embeddings). Native re-extraction of such a page returns
    # empty text, which would otherwise clobber the OCR text and reset
    # its cache row's `source` label back to 'extracted' via the bulk
    # REPLACE in save_pages_text. Stale (mtime-mismatched) rows are
    # already excluded by get_pages_source, so a genuinely modified file
    # still re-extracts and re-writes in full. Merge the OCR text into
    # ``texts`` before embedding so embeddings benefit from it too.
    sources = cache.get_pages_source(path, list(range(page_count)))
    ocr_pages = [pn for pn, src in sources.items() if src == "ocr"]
    preserved: set[int] = set()
    if ocr_pages:
        cached_ocr_text = cache.get_pages_text(path, ocr_pages)
        for pn, cached_text in cached_ocr_text.items():
            if cached_text:
                texts[pn] = cached_text
                preserved.add(pn)

    blobs: dict[int, bytes] = {}
    if embeddings:
        assert embed is not None and model_name is not None
        non_empty = {pn: t for pn, t in texts.items() if t.strip()}
        if non_empty:
            nums = sorted(non_empty)
            vecs = embed([non_empty[pn] for pn in nums])
            blobs = dict(zip(nums, vecs))

    to_save = {pn: t for pn, t in texts.items() if pn not in preserved}

    cache.save_metadata(path, page_count, metadata, toc, text_coverage=coverage)
    cache.save_pages_text(path, to_save)
    if blobs and model_name is not None:
        cache.save_page_embeddings(path, blobs, model_name)
    return page_count


def _warm_one_doc(
    path: str,
    cache: Any,
    embeddings: bool,
    model_name: str | None,
    embed: Callable[[list[str]], list[bytes]] | None,
) -> int:
    """Extract everything for one doc, then write to cache.

    Extraction completes fully before any write, so a failure leaves
    the cache untouched (atomic per doc).
    """
    payload = _warm_extract_worker(path)
    return _finalize_doc(path, *payload, cache, embeddings, model_name, embed)


def warm_docs(
    files: list[str],
    budget_seconds: float,
    cache: Any,
    embeddings: bool = False,
    model_name: str | None = None,
    embed: Callable[[list[str]], list[bytes]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Budgeted warm loop over a resolved corpus.

    Cached docs are free (never charged against the budget). Uncached
    docs warm smallest-first, one at a time, atomically; the clock is
    checked between docs only. Per-doc failures land in ``skipped``
    and never abort the batch. The returned ``docs`` list is sorted by
    path so successive envelopes (first warm vs resume) diff cleanly.

    Each doc row carries ``embeddings_cached``: actual embeddings cache
    state for ``model_name`` (not an echo of the ``embeddings`` request
    flag), so a text-only call still answers whether an embeddings pass
    is needed. Pass ``model_name`` even when ``embeddings`` is False to
    get that report; with ``model_name=None`` it reads False.
    """

    def _emb_cached(path: str) -> bool:
        if model_name is None:
            return False
        return bool(cache.embeddings_complete(path, model_name))

    start = clock()
    docs: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    uncached: list[tuple[str, int]] = []

    for path in files:
        pages = _cached_pages(path, cache, embeddings, model_name)
        if pages is not None:
            docs.append(
                {
                    "path": path,
                    "status": "cached",
                    "pages": pages,
                    "embeddings_cached": _emb_cached(path),
                }
            )
            continue
        try:
            with pymupdf.open(path) as probe:
                uncached.append((path, len(probe)))
        except Exception as e:
            skipped.append({"path": path, "reason": f"unreadable: {e}"})

    uncached.sort(key=lambda item: item[1])

    unprocessed: list[str] = []
    budget_exhausted = False
    warmed = 0
    for i, (path, _pages) in enumerate(uncached):
        if clock() - start > budget_seconds:
            unprocessed = [p for p, _ in uncached[i:]]
            budget_exhausted = True
            break
        try:
            page_count = _warm_one_doc(path, cache, embeddings, model_name, embed)
        except Exception as e:
            skipped.append({"path": path, "reason": f"warm failed: {e}"})
            continue
        warmed += 1
        docs.append(
            {
                "path": path,
                "status": "warmed",
                "pages": page_count,
                "embeddings_cached": _emb_cached(path),
            }
        )

    return {
        "docs": sorted(docs, key=lambda d: str(d["path"])),
        "unprocessed": unprocessed,
        "skipped": skipped,
        "warmed_this_call": warmed,
        "budget_exhausted": budget_exhausted,
    }


def text_coverage_label(coverage: list[dict[str, int]]) -> str:
    """Collapse a per-page coverage map into a triage label.

    "full" when every page has text, "none" when no page does,
    else "partial". An empty map (zero-page doc) reads as "none".
    """
    pages_with_text = sum(1 for c in coverage if c["text_chars"] > 0)
    if coverage and pages_with_text == len(coverage):
        return "full"
    if pages_with_text == 0:
        return "none"
    return "partial"


# Exporter boilerplate that reads as "no title" for triage purposes.
# Matched case-insensitively; "untitled" is a prefix match (iWork
# exports produce e.g. "Untitled 3.pages").
_PLACEHOLDER_TITLES = {"pdf document"}


def _clean_title(title: Any) -> str | None:
    """Null out empty/whitespace and known-placeholder titles."""
    if not isinstance(title, str):
        return None
    stripped = title.strip()
    if not stripped:
        return None
    low = stripped.lower()
    if low in _PLACEHOLDER_TITLES or low.startswith("untitled"):
        return None
    return stripped


def build_overview_card(path: str, cache: Any, from_cache: bool) -> dict[str, Any]:
    """Build one triage card from cached data only (doc must be warm).

    Junk metadata is filtered rather than passed through: whitespace-only
    TOC entries are dropped and placeholder titles read as null, since
    the cards exist for orientation."""
    meta = cache.get_metadata(path)
    toc = meta.get("toc") or []
    title = _clean_title((meta.get("metadata") or {}).get("title"))
    toc_top = [
        (e.get("title") or "").strip()
        for e in toc
        if e["level"] == 1 and (e.get("title") or "").strip()
    ]
    return {
        "path": path,
        "title": title,
        "pages": meta["page_count"],
        "toc_top": toc_top[:8],
        # Post-filter reality: a TOC whose every title is whitespace is
        # junk for orientation and section titling alike, so it reads
        # as no TOC. Any level counts, not just the level-1 preview.
        "has_toc": any((e.get("title") or "").strip() for e in toc),
        "text_coverage": text_coverage_label(meta.get("text_coverage") or []),
        "size_bytes": meta["file_size"],
        "from_cache": from_cache,
    }


def rrf_fuse_doc_rankings(
    rank_lists: list[list[tuple[str, int]]],
    k: int = CORPUS_RRF_K,
    top_k: int | None = None,
    scores: dict[tuple[str, int], float] | None = None,
) -> list[tuple[str, int]]:
    """Fuse per-document rank lists into one global ranking via RRF.

    Each inner list is one document's (doc_path, page) hits, best first.
    Every item appears in exactly one list, so the fused score is
    1 / (k + rank): items interleave by within-document rank.

    That leaves every document's rank-1 page tied at 1/(k+0), and the
    tie covers the whole top of the ranking whenever many documents
    match. `scores` breaks those ties by relevance -- pass the per-page
    BM25 relevance from the per-document search, higher meaning better.
    Without it the tie falls to (doc_path, page), i.e. alphabetical
    order, which carries no relevance at all: a query matching 98 of 100
    documents returned the 10 alphabetically-first ones and scored 0.000
    doc-NDCG, and the same degeneracy silently inflated the stage-2
    spike's spread class, whose labelled documents sorted early.

    BM25 here is computed per document, so scores are not calibrated
    across documents the way a corpus-wide index would calibrate them.
    They are still a real signal -- a document where the query terms are
    rare scores above one where they are boilerplate -- and any relevance
    signal beats sorting by filename. Ranking quality is measured by the
    `described` class in `benchmark_data/corpus_search`.

    Ties in `scores` (and missing entries, which sort last) fall back to
    (doc_path, page) so the result stays deterministic.
    """
    lookup = scores or {}
    scored: list[tuple[float, float, str, int]] = []
    for hits in rank_lists:
        for rank, (doc, page) in enumerate(hits):
            relevance = lookup.get((doc, page), float("-inf"))
            scored.append((1.0 / (k + rank), relevance, doc, page))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2], t[3]))
    fused = [(doc, page) for _s, _r, doc, page in scored]
    return fused[:top_k] if top_k is not None else fused


def rrf_fuse_two_rankings_scored(
    a: list[tuple[str, int]],
    b: list[tuple[str, int]],
    k: int = CORPUS_RRF_K,
    top_k: int | None = None,
) -> list[tuple[tuple[str, int], float]]:
    """RRF across two global rankings (auto mode: keyword + semantic),
    returning each item's fused score alongside it.

    The same (doc_path, page) may appear in both lists; its RRF
    contributions add. Ties break deterministically by (doc_path, page).
    """
    scores: dict[tuple[str, int], float] = {}
    for ranking in (a, b):
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
    return ordered[:top_k] if top_k is not None else ordered


def rrf_fuse_two_rankings(
    a: list[tuple[str, int]],
    b: list[tuple[str, int]],
    k: int = CORPUS_RRF_K,
    top_k: int | None = None,
) -> list[tuple[str, int]]:
    """RRF across two global rankings (auto mode: keyword + semantic).

    The same (doc_path, page) may appear in both lists; its RRF
    contributions add. Ties break deterministically by (doc_path, page).
    """
    ordered = rrf_fuse_two_rankings_scored(a, b, k=k, top_k=top_k)
    return [item for item, _s in ordered]
