"""Corpus resolution and warm orchestration for multi-document tools.

Pure-logic module (no MCP dependency). ``resolve_corpus`` turns a
directory or explicit path list into a validated file list;
``warm_docs`` (Task 2) runs the budgeted warm loop that extracts text
(and optionally embeddings) into the existing per-doc cache. All
SQLite writes go through the ``PDFCache`` instance passed in by the
caller; this module owns no storage of its own.
"""

from __future__ import annotations

import logging
import math
import multiprocessing
import re
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Callable


from .docopen import open_pdf

from .extractor import _warm_extract_worker
from .parallel import resolve_workers

__all__ = [
    "CORPUS_MAX_FILES",
    "CORPUS_RRF_K",
    "CORPUS_DOC_ARM_WEIGHT",
    "PROFILE_HEAD_CHARS",
    "PROFILE_TERM_LIMIT",
    "CORPUS_TERM_RE",
    "resolve_corpus",
    "warm_docs",
    "text_coverage_label",
    "about_terms",
    "build_overview_card",
    "rrf_fuse_doc_rankings",
    "rrf_fuse_rankings_scored",
    "rrf_fuse_two_rankings",
    "rrf_fuse_two_rankings_scored",
    "profile_terms",
    "build_doc_profile",
    "backfill_doc_profiles",
]

logger = logging.getLogger(__name__)

# Hard ceiling on corpus size: the tens-of-docs design boundary made
# explicit. Beyond this, corpus tools return an inline error instead
# of silently truncating.
CORPUS_MAX_FILES = 100

# RRF constant for cross-document fusion; matches server._RRF_K so corpus
# fusion and single-doc hybrid fusion share one k. Design decided by the
# stage-2 ranking benchmark: per-document fusion, not corpus-wide FTS.
CORPUS_RRF_K = 60

# Weight of the document arm in hybrid corpus fusion. Measured on the 500-doc
# distractor rung (spike 2026-08-26, race 2): head vector at 0.25 and 0.5 are
# indistinguishable (described hit@3 0.68, needle 1.000, trap 0.985); 0.25 is
# the lighter touch and keeps 100-doc doc-NDCG flat. An equal-weight (1.0)
# third list dilutes needles the page arms had already nailed. Not a tool
# parameter: re-measure, never tune.
CORPUS_DOC_ARM_WEIGHT = 0.25
# Head text = page 1's first N characters: title, authors and abstract on
# arXiv papers; cover plus summary on a 10-K. From the spike; not tuned.
PROFILE_HEAD_CHARS = 1500
PROFILE_TERM_LIMIT = 200
# Latin word tokens. Shared with server._corpus_query_terms so profile terms
# and query terms agree on what a term is; 4+ chars filters function words.
CORPUS_TERM_RE = re.compile(r"[a-z0-9]+")

# Concurrent-warm pool sizing (benchmark: warm_concurrency_results.md).
# Below the gate, sequential is faster (spawn/IPC overhead outweighs the
# win). Text warm scales to 8 workers (3.87x); embeddings warm is
# encode-bound and plateaus at ~4 workers (1.62x; extra processes
# oversubscribe the encode's own threads).
WARM_DOC_GATE = 4
WARM_TEXT_CAP = 8
WARM_EMBED_CAP = 4


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


def profile_terms(texts: dict[int, str]) -> dict[str, int]:
    """Top PROFILE_TERM_LIMIT tokens (4+ chars, lowercase) by count across
    all pages. Ties break by term so the result is deterministic."""
    counts: dict[str, int] = {}
    for text in texts.values():
        for tok in CORPUS_TERM_RE.findall(text.lower()):
            if len(tok) > 3:
                counts[tok] = counts.get(tok, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(top[:PROFILE_TERM_LIMIT])


def build_doc_profile(
    texts: dict[int, str],
    embed: Callable[[list[str]], list[bytes]],
) -> "tuple[bytes | None, dict[str, int]]":
    """The per-document profile: (head vector or None, term counts).

    The head vector embeds page 1's first PROFILE_HEAD_CHARS characters
    with the same callable used for pages, so it lives in the same space
    as the page embeddings and the query. No text on page 1 means no
    vector (None), never a vector of an empty string.
    """
    head = (texts.get(0) or "")[:PROFILE_HEAD_CHARS]
    vec: bytes | None = None
    if head.strip():
        vec = embed([head])[0]
    return vec, profile_terms(texts)


def backfill_doc_profiles(
    paths: list[str],
    cache: Any,
    model_name: str,
    embed: Callable[[list[str]], list[bytes]],
) -> int:
    """Write profiles for warm docs that lack a valid one, from cached text.

    Covers caches warmed before profiles existed, and model changes. Reads
    only page_text (no PDF open, no page re-embed); one encode per doc
    (batching measured no faster); all new rows in one transaction.
    Returns how many rows were written. A doc whose encode raises is
    logged and skipped so the rest still land.
    """
    have = cache.get_doc_profiles(paths, model_name)
    pending = [p for p in paths if p not in have]
    if not pending:
        return 0
    built: list[tuple[str, "bytes | None", dict[str, int]]] = []
    for path in pending:
        meta = cache.get_metadata(path)
        if meta is None:
            continue
        texts = cache.get_pages_text(path, list(range(meta["page_count"])))
        if not texts:
            continue
        try:
            vec, terms = build_doc_profile(texts, embed)
        except Exception as exc:  # noqa: BLE001 - never a search/warm error
            logger.warning("doc profile backfill skipped %s: %s", path, exc)
            continue
        built.append((path, vec, terms))
    if not built:
        return 0
    with cache.write_transaction() as conn:
        for path, vec, terms in built:
            cache.save_doc_profile(
                path, PROFILE_HEAD_CHARS, vec, terms, model_name, conn=conn
            )
    return len(built)


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
    layout: "dict[int, tuple[list[Any], tuple[float, float], bool]] | None" = None,
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

    profile: "tuple[bytes | None, dict[str, int]] | None" = None
    if embeddings and embed is not None and model_name is not None:
        try:
            profile = build_doc_profile(texts, embed)
        except Exception as exc:  # noqa: BLE001 - profile is optional
            logger.warning("doc profile skipped for %s: %s", path, exc)

    # ONE transaction for the whole document. Each of these writes used to
    # open its own connection, and leaving that block commits, which is an
    # fsync. Measured on same-spec CI runners, warming a 6-document corpus
    # spent 3.18s of 3.39s in commits on Windows against 0.05s on Linux,
    # while extraction itself was FASTER on Windows (0.205s vs 0.253s). So
    # warm was dominated by durability barriers, not by work.
    #
    # It is also more correct: a document's metadata, text, embeddings and
    # layout now land together or not at all, which is the atomicity
    # _warm_one_doc already aims for by extracting fully before writing.
    with cache.write_transaction() as conn:
        cache.save_metadata(
            path, page_count, metadata, toc, text_coverage=coverage, conn=conn
        )
        cache.save_pages_text(path, to_save, conn=conn)
        if blobs and model_name is not None:
            cache.save_page_embeddings(path, blobs, model_name, conn=conn)
        if profile is not None and model_name is not None:
            cache.save_doc_profile(
                path, PROFILE_HEAD_CHARS, profile[0], profile[1], model_name, conn=conn
            )
        if layout:
            # Written AFTER page_text: the hidden flag lives on page_text
            # rows, and blocks are keyed independently by mtime.
            cache.save_page_blocks(
                path,
                {pn: (blocks, size) for pn, (blocks, size, _h) in layout.items()},
                conn=conn,
            )
            cache.save_pages_hidden_flag(
                path,
                {pn: hidden for pn, (_b, _s, hidden) in layout.items()},
                conn=conn,
            )
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
    page_count, metadata, toc, texts, coverage, layout = _warm_extract_worker(path)
    return _finalize_doc(
        path,
        page_count,
        metadata,
        toc,
        texts,
        coverage,
        cache,
        embeddings,
        model_name,
        embed,
        layout=layout,
    )


def _warm_worker_count(n_uncached: int, embeddings: bool) -> int:
    """Pool size for warming, or 1 for sequential (gate/env via
    parallel.resolve_workers, mode-dependent cap)."""
    cap = WARM_EMBED_CAP if embeddings else WARM_TEXT_CAP
    return resolve_workers(n_uncached, WARM_DOC_GATE, cap=cap)


def _warm_sequential(
    pending: list[tuple[str, int]],
    budget_seconds: float,
    start: float,
    clock: Callable[[], float],
    cache: Any,
    embeddings: bool,
    model_name: str | None,
    embed: Callable[[list[str]], list[bytes]] | None,
    docs: list[dict[str, Any]],
    skipped: list[dict[str, str]],
    emb_cached: Callable[[str], bool],
) -> tuple[list[str], bool, int]:
    """Sequential warm loop (today's semantics, verbatim): clock checked
    before each doc; per-doc failure -> skipped; appends to docs/skipped
    in place. Returns (unprocessed, budget_exhausted, warmed)."""
    warmed = 0
    unprocessed: list[str] = []
    budget_exhausted = False
    for i, (path, _pages) in enumerate(pending):
        if clock() - start > budget_seconds:
            unprocessed = [p for p, _ in pending[i:]]
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
                "embeddings_cached": emb_cached(path),
            }
        )
    return unprocessed, budget_exhausted, warmed


def _warm_concurrent(
    uncached: list[tuple[str, int]],
    workers: int,
    budget_seconds: float,
    start: float,
    clock: Callable[[], float],
    cache: Any,
    embeddings: bool,
    model_name: str | None,
    embed: Callable[[list[str]], list[bytes]] | None,
    docs: list[dict[str, Any]],
    skipped: list[dict[str, str]],
    emb_cached: Callable[[str], bool],
) -> tuple[list[str], bool, int]:
    """Pool-scheduled warm: extraction in spawn workers, finalize in parent.

    Spawn context explicitly, on every OS: uniform cross-platform
    behavior, matches the benchmark, and avoids forking a parent whose
    onnxruntime threads (embeddings model) are not fork-safe. Budget is
    checked before each submission; on expiry in-flight docs drain and
    finalize (overshoot bounded by the worker count). Workers never
    touch SQLite. On BrokenProcessPool, or an OSError from pool
    creation/submission or a worker dying on an OS-level failure
    (fd/semaphore exhaustion -- EMFILE/EAGAIN surface here, not as
    BrokenProcessPool, since workers spawn lazily inside submit()),
    the un-handled remainder finishes sequentially in-parent rather
    than escaping the tool.
    """
    warmed = 0
    pending = list(uncached)
    unprocessed: list[str] = []
    budget_exhausted = False
    handled: set[str] = set()
    try:
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            in_flight: dict[Future[Any], str] = {}
            while pending or in_flight:
                while pending and len(in_flight) < workers:
                    if clock() - start > budget_seconds:
                        budget_exhausted = True
                        unprocessed = [p for p, _ in pending]
                        pending = []
                        break
                    path, _pages = pending.pop(0)
                    fut = pool.submit(_warm_extract_worker, path)
                    in_flight[fut] = path
                if not in_flight:
                    break
                done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                for fut in done:
                    path = in_flight.pop(fut)
                    try:
                        (
                            page_count_w,
                            metadata_w,
                            toc_w,
                            texts_w,
                            coverage_w,
                            layout_w,
                        ) = fut.result()
                        page_count = _finalize_doc(
                            path,
                            page_count_w,
                            metadata_w,
                            toc_w,
                            texts_w,
                            coverage_w,
                            cache,
                            embeddings,
                            model_name,
                            embed,
                            layout=layout_w,
                        )
                    except BrokenProcessPool:
                        raise
                    except OSError:
                        # Worker died on an OS-level failure (not a pool
                        # crash): treat like BrokenProcessPool -- re-raise
                        # to trigger the sequential fallback below rather
                        # than silently skipping just this doc, since the
                        # same OS pressure likely affects the rest of the
                        # pool too.
                        raise
                    except Exception as e:
                        handled.add(path)
                        skipped.append({"path": path, "reason": f"warm failed: {e}"})
                        continue
                    handled.add(path)
                    warmed += 1
                    docs.append(
                        {
                            "path": path,
                            "status": "warmed",
                            "pages": page_count,
                            "embeddings_cached": emb_cached(path),
                        }
                    )
    except (BrokenProcessPool, OSError):
        # Leaving the `with` block joins in-flight workers (shutdown(wait=
        # True)); their partial results are discarded and those docs are
        # re-extracted sequentially below. Bounded by the worker count,
        # rare path, accepted.
        remaining = [item for item in uncached if item[0] not in handled]
        unprocessed, budget_exhausted, seq_warmed = _warm_sequential(
            remaining,
            budget_seconds,
            start,
            clock,
            cache,
            embeddings,
            model_name,
            embed,
            docs,
            skipped,
            emb_cached,
        )
        return unprocessed, budget_exhausted, warmed + seq_warmed
    return unprocessed, budget_exhausted, warmed


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
    docs warm smallest-first, atomically per doc; the clock is
    checked between docs (sequential) or before each submission
    (concurrent). When the budget expires mid-pool, extractions already
    in flight still complete and are written, so overshoot is bounded
    by the worker count rather than a single doc. Per-doc failures land
    in ``skipped`` and never abort the batch. The returned ``docs``
    list is sorted by path so successive envelopes (first warm vs
    resume) diff cleanly.

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
            with open_pdf(path) as probe:
                uncached.append((path, len(probe)))
        except Exception as e:
            skipped.append({"path": path, "reason": f"unreadable: {e}"})

    # Profiles are new relative to existing caches: an explicit embeddings
    # warm repairs coverage on docs that are otherwise fully cached, so a
    # caller who warms gets the document arm without a search. Cheap
    # (~17ms per doc) and never charged against the budget, like cached
    # docs themselves.
    if embeddings and embed is not None and model_name is not None:
        cached_paths = [d["path"] for d in docs if d["status"] == "cached"]
        try:
            backfill_doc_profiles(cached_paths, cache, model_name, embed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("doc profile backfill failed: %s", exc)

    uncached.sort(key=lambda item: item[1])
    workers = _warm_worker_count(len(uncached), embeddings)
    if workers <= 1:
        unprocessed, budget_exhausted, warmed = _warm_sequential(
            uncached,
            budget_seconds,
            start,
            clock,
            cache,
            embeddings,
            model_name,
            embed,
            docs,
            skipped,
            _emb_cached,
        )
    else:
        unprocessed, budget_exhausted, warmed = _warm_concurrent(
            uncached,
            workers,
            budget_seconds,
            start,
            clock,
            cache,
            embeddings,
            model_name,
            embed,
            docs,
            skipped,
            _emb_cached,
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


def about_terms(cache: Any, paths: list[str], limit: int = 8) -> dict[str, list[str]]:
    """Each document's most distinctive stored terms relative to the corpus.

    tf * log(1 + N / df) over the cached term lists only (no page text
    read). A readability aid on overview cards, validated by inspection;
    it makes no retrieval claim. Latin tokens only: CJK documents get an
    empty list (documented limitation).
    """
    terms_by_doc = cache.get_doc_terms(paths)
    n_docs = len(paths)
    df: dict[str, int] = {}
    for terms in terms_by_doc.values():
        for t in terms:
            df[t] = df.get(t, 0) + 1
    out: dict[str, list[str]] = {}
    for path in paths:
        terms = terms_by_doc.get(path)
        if not terms:
            out[path] = []
            continue
        ranked = sorted(
            terms.items(),
            key=lambda kv: (-kv[1] * math.log(1.0 + n_docs / df[kv[0]]), kv[0]),
        )
        out[path] = [t for t, _c in ranked[:limit]]
    return out


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
    # Scanner/exporter artifacts: underscore-wrapped (e.g. _WATERMARKED_)
    # or an embedded triple-underscore marker (real field sample:
    # "506673___CLEANLPDF_LAN_...PDF"). Three underscores, not two, so
    # legitimate titles mentioning dunder names (__init__) survive.
    if stripped.startswith("_") and stripped.endswith("_"):
        return None
    if "___" in stripped:
        return None
    return stripped


def build_overview_card(
    path: str,
    cache: Any,
    from_cache: bool,
    about: list[str] | None = None,
) -> dict[str, Any]:
    """Build one triage card from cached data only (doc must be warm).

    Junk metadata is filtered rather than passed through: whitespace-only
    TOC entries are dropped and placeholder titles fall back to the
    filename stem (never null, unified with search's doc_title), since
    the cards exist for orientation."""
    meta = cache.get_metadata(path)
    toc = meta.get("toc") or []
    title = _clean_title((meta.get("metadata") or {}).get("title")) or Path(path).stem
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
        "about": list(about or []),
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


def rrf_fuse_rankings_scored(
    rankings: list[tuple[list[tuple[str, int]], float]],
    k: int = CORPUS_RRF_K,
    top_k: int | None = None,
) -> list[tuple[tuple[str, int], float]]:
    """Weighted RRF across N global rankings, returning fused scores.

    Each entry is (ranking, weight); an item's score is the sum of
    weight / (k + rank) over every list it appears in (Cormack et al.
    2009, with the per-list weight extension). Hybrid corpus search
    passes [(keyword, 1.0), (semantic, 1.0), (doc_arm,
    CORPUS_DOC_ARM_WEIGHT)]. Ties break by (doc_path, page), so a
    document rename never reorders results except at exact ties.
    """
    scores: dict[tuple[str, int], float] = {}
    for ranking, weight in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + weight / (k + rank)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
    return ordered[:top_k] if top_k is not None else ordered


def rrf_fuse_two_rankings_scored(
    a: list[tuple[str, int]],
    b: list[tuple[str, int]],
    k: int = CORPUS_RRF_K,
    top_k: int | None = None,
) -> list[tuple[tuple[str, int], float]]:
    """Two-list RRF at weight 1.0 each (single-doc hybrid and the keyword
    paths). A wrapper over rrf_fuse_rankings_scored so both stay
    byte-identical to the pre-document-arm fusion."""
    return rrf_fuse_rankings_scored([(a, 1.0), (b, 1.0)], k=k, top_k=top_k)


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
