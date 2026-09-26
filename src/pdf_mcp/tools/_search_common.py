"""Excerpt, window and span helpers shared by the search tools."""

import re
from typing import Any, Callable
from ..backend.geometry import Rect as GeomRect
from .. import corpus
from ..extractor import (
    block_bbox_for_index,
    get_best_paragraph_for_query,
)
from .. import _core
from ._render import _bbox_to_clip

# ============================================================================
# Tool 4: pdf_search - Search within PDF
# ============================================================================


def _python_search(
    page_texts: dict[int, str],
    query: str,
    max_results: int,
    context_chars: int,
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """
    Python token-matching fallback for pdf_search when FTS5 is unavailable.

    Tokenises the query on whitespace and requires every token to appear
    on the page (case-insensitive, order-independent). Page counts reflect
    total token occurrences across the page; the excerpt is centred on the
    first token hit found.

    Returns (matches, page_counts) where:
    - matches: list of {page, excerpt, position, score} (score=0.0)
    - page_counts: dict mapping 0-indexed page_num to total token-occurrence count
    """
    matches: list[dict[str, Any]] = []
    page_counts: dict[int, int] = {}
    tokens_lower = [t for t in query.lower().split() if t]
    if not tokens_lower:
        return matches, page_counts

    for page_num, text in sorted(page_texts.items()):
        text_lower = text.lower()
        token_counts = [text_lower.count(t) for t in tokens_lower]
        if not all(c > 0 for c in token_counts):
            continue

        page_counts[page_num] = sum(token_counts)

        if len(matches) >= max_results:
            continue

        first_token = tokens_lower[0]
        pos = text_lower.find(first_token)
        ctx_start = max(0, pos - context_chars // 2)
        ctx_end = min(len(text), pos + len(first_token) + context_chars // 2)

        if ctx_start > 0:
            space_pos = text.rfind(" ", ctx_start - 50, ctx_start)
            if space_pos > 0:
                ctx_start = space_pos + 1

        if ctx_end < len(text):
            space_pos = text.find(" ", ctx_end, ctx_end + 50)
            if space_pos > 0:
                ctx_end = space_pos

        excerpt = text[ctx_start:ctx_end]
        if ctx_start > 0:
            excerpt = "..." + excerpt
        if ctx_end < len(text):
            excerpt = excerpt + "..."

        matches.append(
            {
                "page": page_num + 1,
                "excerpt": excerpt.strip(),
                "position": pos,
                "score": 0.0,
            }
        )

    return matches, page_counts


class _LayoutPage:
    """Page stand-in built from the cached blocks shape.

    The excerpt-upgrade path reads exactly two things from a page: the
    sorted text-blocks shape and the page rect. When warm has persisted
    both, a query builds its paragraph excerpts without opening the PDF
    at all, which is what puts the corpus query path AHEAD of the
    PyMuPDF baseline instead of chasing it: the baseline still pays live
    extraction per hit page.
    """

    def __init__(self, blocks: list[Any], size: tuple[float, float]) -> None:
        self._blocks = blocks
        self.rect = GeomRect(0.0, 0.0, size[0], size[1])

    def get_text(self, kind: str = "text", **_kw: Any) -> Any:
        if kind != "blocks":
            raise ValueError(f"_LayoutPage serves only 'blocks', not {kind!r}")
        return self._blocks


def _layout_page(doc: Any, page_num_0: int) -> Any:
    """Cached-blocks page when available; live page (with write-through)
    otherwise. Best-effort: any failure falls back to the live page."""
    try:
        local_path = getattr(doc, "name", None)
        if not local_path:
            return doc[page_num_0]
        cached = _core.cache.get_page_blocks(local_path, page_num_0)
        if cached is not None:
            return _LayoutPage(*cached)
        page = doc[page_num_0]
        blocks = [tuple(b) for b in page.get_text("blocks", sort=True)]
        rect = page.rect
        _core.cache.save_page_blocks(
            local_path,
            {page_num_0: (blocks, (float(rect.width), float(rect.height)))},
        )
        return _LayoutPage(blocks, (float(rect.width), float(rect.height)))
    except Exception:  # noqa: BLE001
        return doc[page_num_0]


def _upgrade_excerpts_to_paragraphs(
    matches: list[dict[str, Any]],
    doc: Any,
    query: str,
    keyword_excerpts: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Replace windowed snippet excerpts with structural text blocks.

    When *keyword_excerpts* maps a 0-indexed page number to an FTS5
    snippet, the block containing that snippet is preferred (direct
    containment check).  Otherwise falls back to
    ``get_best_paragraph_for_query`` (query-token overlap).

    Short blocks (headings, captions) are caught by a minimum-length
    floor: if the chosen block is under ``_PARAGRAPH_MIN_CHARS``, the
    picker retries with the floor applied so only substantive blocks
    are candidates.  The retry result is kept only when it covers at
    least as many query tokens as the short block; otherwise the short
    matching block wins, so a table-cell hit is never traded for a
    nearby prose block that merely shares one term (and its geometry
    keeps pointing at the true hit region).

    Deduplicates matches sharing the same (page, block_index).  Falls
    back to the original snippet when the block exceeds the cap or
    can't be located.
    """
    from ..extractor import _PARAGRAPH_MIN_CHARS, count_query_tokens

    seen: dict[tuple[int, int], int] = {}  # (page, block_idx) -> index in upgraded
    upgraded: list[dict[str, Any]] = []

    for m in matches:
        lazy_excerpt = m.pop("_lazy_excerpt", None)
        page_num_0 = m["page"] - 1
        page = _layout_page(doc, page_num_0)

        block_text: str | None = None
        block_idx: int | None = None

        if keyword_excerpts is not None and page_num_0 in keyword_excerpts:
            fragment = keyword_excerpts[page_num_0].replace("...", "").strip()
            if fragment:
                blocks = page.get_text("blocks", sort=True)
                text_blocks = [b[4] for b in blocks if b[6] == 0]
                # Whitespace-normalised containment: the FTS excerpt is a
                # window over page_text, whose line joins differ from the
                # blocks shape (spaces vs newlines around table cells), so
                # a literal substring test failed on exactly the matches
                # this anchor exists for. The query-dense-block fallback
                # then picked an intro sentence over the table row holding
                # the answer (Berkshire p67, "manufacturing revenues").
                fragment_norm = " ".join(fragment.split())
                for idx, bt in enumerate(text_blocks):
                    if fragment_norm in " ".join(bt.split()):
                        stripped = bt.strip()
                        if len(stripped) <= 2000:
                            block_text = stripped
                            block_idx = idx
                        break

        if block_text is None:
            block_text, block_idx = get_best_paragraph_for_query(page, query)

        if block_text is not None and len(block_text) < _PARAGRAPH_MIN_CHARS:
            alt_text, alt_idx = get_best_paragraph_for_query(
                page, query, min_chars=_PARAGRAPH_MIN_CHARS
            )
            if (
                alt_text is not None
                and alt_idx is not None
                and count_query_tokens(alt_text, query)
                >= count_query_tokens(block_text, query)
            ):
                block_text, block_idx = alt_text, alt_idx

        if block_text is not None and block_idx is not None:
            geom: dict[str, Any] = {}
            bbox = block_bbox_for_index(page, block_idx)
            if bbox is not None:
                r = page.rect
                page_rect = [
                    round(r.x0, 1),
                    round(r.y0, 1),
                    round(r.x1, 1),
                    round(r.y1, 1),
                ]
                geom = {
                    "bbox": list(bbox),
                    "page_rect": page_rect,
                    "clip": _bbox_to_clip(bbox, page_rect),
                }
            key = (m["page"], block_idx)
            if key in seen:
                existing_idx = seen[key]
                if m.get("score", 0) > upgraded[existing_idx].get("score", 0):
                    upgraded[existing_idx] = {**m, "excerpt": block_text, **geom}
                continue
            seen[key] = len(upgraded)
            upgraded.append({**m, "excerpt": block_text, **geom})
        else:
            upgraded.append(_resolve_lazy_excerpt(m, lazy_excerpt))

    return upgraded


def _resolve_lazy_excerpt(
    m: dict[str, Any], lazy_excerpt: Callable[[], str] | None
) -> dict[str, Any]:
    """Fill a match's excerpt from its deferred span computation, if any.

    Paragraph and window styles build their own excerpt from text blocks
    and only fall back to the incoming one on a blockless page (scanned,
    OCR text). For semantic-only hits that incoming excerpt is the
    anchored span, whose search encodes candidate windows and doubled
    pure-semantic paragraph latency when computed for every hit up
    front. Those hits therefore arrive with `excerpt: ""` plus a
    `_lazy_excerpt` thunk that the two fallback branches resolve here.
    Snippet style stays eager.
    """
    if lazy_excerpt is None:
        return m
    return {**m, "excerpt": lazy_excerpt()}


def _semantic_excerpt_fields(
    excerpt_style: str,
    path: str,
    page0: int,
    query: str,
    query_vec: Any,
    model: str,
    context_chars: int,
    chunk: Callable[[], str | None],
) -> dict[str, Any]:
    """Excerpt fields for a semantic-only hit: the anchored span now for
    snippet style, deferred (see `_resolve_lazy_excerpt`) for paragraph
    and window. `chunk` returns the page's best sub-page unit text and is
    called only when the span is actually computed."""

    def span() -> str:
        return _semantic_snippet_excerpt(
            path, page0, query, query_vec, model, context_chars, chunk()
        )

    if excerpt_style in ("paragraph", "window"):
        return {"excerpt": "", "_lazy_excerpt": span}
    return {"excerpt": span()}


def _attach_snippet_geometry(
    matches: list[dict[str, Any]], doc: Any
) -> list[dict[str, Any]]:
    """Attach bbox/page_rect/clip to snippet hits WITHOUT changing their
    excerpts (F6, 2026-09-01 consumer tryout: auto-routed snippets carried
    no geometry, breaking render-and-cite exactly on the multi-document
    case). The block containing the excerpt is found by whitespace-
    normalised containment (same trick as the paragraph upgrade); an
    excerpt spanning block boundaries falls back to its middle fragment.
    Fields are omitted when no block can be located -- absence is the
    signal, matching the paragraph contract."""
    out: list[dict[str, Any]] = []
    for m in matches:
        excerpt = m.get("excerpt") or ""
        fragment = excerpt.replace("...", "").strip()
        if not fragment:
            out.append(m)
            continue
        page = _layout_page(doc, m["page"] - 1)
        blocks = page.get_text("blocks", sort=True)
        text_blocks = [b[4] for b in blocks if b[6] == 0]
        norm_blocks = [" ".join(bt.split()) for bt in text_blocks]

        def _find(cand: str) -> int | None:
            for idx, bt in enumerate(norm_blocks):
                if cand and cand in bt:
                    return idx
            return None

        # Round-3 refinement 2: locate the excerpt's head and tail
        # separately; a spanning excerpt gets the UNION of the located
        # block range, so a clip crop shows everything the excerpt shows
        # (measured: 39% of snippet excerpts span blocks; anchor-only
        # bbox under-covered exactly those).
        # Head/tail probes must not themselves cross a block boundary:
        # take the first and last LINE of the excerpt (capped), since
        # extracted text breaks lines at block edges.
        lines = [ln for ln in fragment.splitlines() if ln.strip()]
        head_idx = _find(" ".join(lines[0][:60].split())) if lines else None
        tail_idx = _find(" ".join(lines[-1][-60:].split())) if lines else None
        located = [i for i in (head_idx, tail_idx) if i is not None]
        if not located and len(fragment) > 80:
            mid = len(fragment) // 2
            mid_idx = _find(" ".join(fragment[mid - 30 : mid + 30].split()))
            if mid_idx is not None:
                located = [mid_idx]
        if not located:
            # Round-3 refinement 3: say so, instead of silent absence.
            out.append({**m, "geometry": "unlocatable"})
            continue
        boxes = [
            b
            for i in range(min(located), max(located) + 1)
            if (b := block_bbox_for_index(page, i)) is not None
        ]
        if not boxes:
            out.append({**m, "geometry": "unlocatable"})
            continue
        # Round-4 finding: a line probe can match the WRONG block (the
        # cached column-aware text stream segments differently from the
        # layout blocks), yielding a bbox that does not overlap the
        # quoted text at all. Never emit an unvalidated bbox: crop the
        # candidate rect, re-extract, and require a fragment of the
        # excerpt inside. Degrade union -> best single located block
        # (marked "partial" when it passes but covers less than the
        # excerpt) -> "unlocatable".
        mid_i = len(fragment) // 2
        probes = [
            " ".join(fragment[mid_i - 30 : mid_i + 30].split()),
            " ".join(fragment[:50].split()),
        ]

        raw_page = doc[m["page"] - 1]

        def _validated(rect: list[float]) -> bool:
            try:
                crop = " ".join(raw_page.get_text(clip=GeomRect(*rect)).split())
            except Exception:
                return False
            return any(pr and pr in crop for pr in probes)

        union = [
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        ]
        chosen: list[float] | None = None
        partial = False
        if _validated(union):
            chosen = union
        else:
            for i in located:
                b = block_bbox_for_index(page, i)
                if b is not None and _validated(list(b)):
                    chosen = list(b)
                    partial = True
                    break
        if chosen is None:
            out.append({**m, "geometry": "unlocatable"})
            continue
        r = page.rect
        page_rect = [round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)]
        out.append(
            {
                **m,
                "bbox": list(chosen),
                "page_rect": page_rect,
                "clip": _bbox_to_clip(chosen, page_rect),
                **({"geometry": "partial"} if partial else {}),
            }
        )
    return out


_WINDOW_TOKENS_DEFAULT = 600


def _best_span_in_text(
    text: str,
    query_vec: Any,
    embed_model: str,
    context_chars: int,
    query: str = "",
    max_windows: int = 12,
) -> str:
    """Best `context_chars`-sized window of `text` for the query, used to
    position a snippet excerpt inside a semantically-matched page when
    there is no keyword hit to anchor on (2026-09-01 consumer tryout,
    F1). Candidates are a stride grid plus a window centred on each
    literal query-term occurrence; when any candidate contains a query
    term, only those candidates compete (lexical back-off), ranked by
    cosine against the query vector with the retrieval encoder. Reason:
    pure cosine at this granularity can prefer citation/header soup over
    the window holding the query's own term (re-verification residual,
    2607.08291 p34). Term-free paraphrase queries are unaffected by
    construction. Fail-safe: any error returns the head of the text."""
    try:
        if len(text) <= context_chars:
            return text
        stride = max(context_chars // 2, 1)
        starts = list(range(0, len(text) - context_chars + 1, stride))
        if len(starts) > max_windows:
            step = len(starts) / max_windows
            starts = [starts[int(i * step)] for i in range(max_windows)]
        low = text.lower()
        terms = _corpus_query_terms(query) if query else set()
        term_starts: list[int] = []
        for term in terms:
            pos = low.find(term)
            while pos != -1 and len(term_starts) < max_windows:
                st = min(max(pos - context_chars // 2, 0), len(text) - context_chars)
                term_starts.append(st)
                pos = low.find(term, pos + len(term))
        starts = sorted(set(starts) | set(term_starts))
        windows = [text[st : st + context_chars] for st in starts]
        if terms:
            with_term = [w for w in windows if any(t in w.lower() for t in terms)]
            if with_term:
                windows = with_term
        from .. import embedder as _emb

        import numpy as np

        vecs = _emb.encode(windows, embed_model)
        q = np.asarray(query_vec, dtype=np.float32)
        qn = q / (np.linalg.norm(q) or 1.0)
        best_i, best_s = 0, float("-inf")
        for i, v in enumerate(vecs):
            v = np.asarray(v, dtype=np.float32)
            sim = float(np.dot(qn, v / (np.linalg.norm(v) or 1.0)))
            if sim > best_s:
                best_i, best_s = i, sim
        return _snap_window_start(text, windows[best_i], terms, context_chars)
    except Exception:
        return text[:context_chars]


# A period preceded by a lone capital ("Appendix A. ...") is an
# enumerator, not a sentence end; skip it or the snap lands mid-title.
_SENTENCE_BOUNDARY = re.compile(r"(?:(?<=[^A-Z])\.\s+|\n\n|\n(?=[A-Z0-9]))")


def _snap_window_start(
    text: str, window: str, terms: set[str], context_chars: int
) -> str:
    """Round-3 refinement 1 (v2 rule): open the excerpt at the last
    sentence/paragraph boundary BEFORE the first in-window query term, so
    it leads with the on-topic sentence instead of the tail of the
    previous one (p34: opens at "Appendix A. Simulation Details" rather
    than mid-citation). Termless windows snap to the first boundary in
    the leading 40% (measured: clean-start 15.8% -> 47.6%, term
    relevance unchanged). The snap keeps the excerpt length by extending
    the end, and is skipped rather than ever dropping the last query
    term. Fail-safe: any surprise returns the window unchanged."""
    try:
        start = text.find(window)
        if start < 0:
            return window
        low = window.lower()
        first_term = min((low.find(t) for t in terms if t in low), default=-1)
        if first_term > 0:
            bounds = [
                m.end() for m in _SENTENCE_BOUNDARY.finditer(window, 0, first_term)
            ]
            if not bounds:
                return window
            off = bounds[-1]
        elif first_term == 0:
            return window
        else:
            m = _SENTENCE_BOUNDARY.search(window, 0, int(context_chars * 0.4))
            if not m:
                return window
            off = m.end()
        cand = text[start + off : start + off + context_chars]
        if terms and any(t in low for t in terms):
            if not any(t in cand.lower() for t in terms):
                return window
        return cand
    except Exception:
        return window


def _route_evidence_unit(keyword_docs: int) -> str:
    """Per-query excerpt unit for `excerpt_style="auto"`, keyed on the
    number of documents holding an AND keyword match (the count the
    hybrid path already computes, no OR fallback). 0 means a paraphrase
    question (paragraph is its best-measured unit); 1 means a single-
    document keyword question (one contiguous window covers it); 2+
    means the answer is spread across documents (many small units cover
    more of them at the same budget). This is the Bedrock-anchor
    `P-auto` arm; the mapping and the harness must not drift apart."""
    if keyword_docs <= 0:
        return "paragraph"
    if keyword_docs == 1:
        return "window"
    return "snippet"


def _route_excerpt_auto(
    keyword_docs: int, window_tokens: int
) -> tuple[str, dict[str, Any]]:
    """Resolve `excerpt_style="auto"` for one corpus query: the effective
    style plus the `excerpt_routing` response payload (sans
    `matching_doc_count`, which the caller fills at response time).
    `reason` is the human-readable audit trail an agent can pass on to
    its user; `window_tokens_applied` is null unless the window branch
    actually consumed it (2026-09-01 consumer-tryout feedback: echoing
    an unused number reads as "N tokens were allocated")."""
    unit = _route_evidence_unit(keyword_docs)
    if keyword_docs == 0:
        reason = (
            "0 documents carry every keyword term (paraphrase query)"
            " -> paragraph excerpts"
        )
    elif keyword_docs == 1:
        reason = (
            "1 document carries every keyword term -> one"
            f" {window_tokens}-token window per hit"
        )
    else:
        reason = (
            f"{keyword_docs} documents carry every keyword term (answer"
            " likely spread) -> snippet excerpts, more documents per"
            " token"
        )
    routing = {
        "unit": unit,
        "reason": reason,
        "keyword_doc_count": keyword_docs,
        "window_tokens_applied": window_tokens if unit == "window" else None,
    }
    return unit, routing


def _expand_block_window(sizes: list[int], anchor: int, budget: int) -> tuple[int, int]:
    """Widest contiguous block range around `anchor` whose token cost stays
    within `budget`, growing alternately forward and backward. The anchor
    block is always included, even over budget."""
    lo = hi = anchor
    used = sizes[anchor]
    forward = True
    while True:
        moved = False
        for _ in range(2):
            if forward:
                if hi + 1 < len(sizes) and used + sizes[hi + 1] <= budget:
                    hi += 1
                    used += sizes[hi]
                    moved = True
            else:
                if lo - 1 >= 0 and used + sizes[lo - 1] <= budget:
                    lo -= 1
                    used += sizes[lo]
                    moved = True
            forward = not forward
        if not moved:
            return lo, hi


def _block_best_covering(texts: list[str], fragment: str) -> int | None:
    """Index of the block that shares the most whitespace-normalised
    text with `fragment` (a sub-page embedding chunk), or None when no
    block overlaps it at all."""
    frag = " ".join(fragment.split())
    if not frag:
        return None
    best: int | None = None
    best_len = 0
    for idx, t in enumerate(texts):
        norm = " ".join(t.split())
        if not norm:
            continue
        if norm in frag:
            covered = len(norm)
        elif frag in norm:
            covered = len(frag)
        else:
            # partial overlap at either end of the chunk
            covered = 0
            for k in range(min(len(norm), len(frag)), 39, -1):
                if norm[:k] == frag[-k:] or norm[-k:] == frag[:k]:
                    covered = k
                    break
        if covered > best_len:
            best_len, best = covered, idx
    return best


def _expand_excerpts_to_windows(
    matches: list[dict[str, Any]],
    doc: Any,
    query: str,
    keyword_excerpts: dict[int, str] | None = None,
    window_tokens: int = _WINDOW_TOKENS_DEFAULT,
) -> list[dict[str, Any]]:
    """Replace each match's excerpt with a contiguous window of text
    blocks around an anchor, up to `window_tokens` (~4 chars per token).

    Anchor, in priority order: the block containing the FTS keyword
    excerpt (`anchor: "keyword"`); the block best covered by the page's
    best-scoring sub-page embedding chunk, carried on the match as
    `_best_chunk` (`"semantic"`); the block with the most query-token
    hits (`"query_terms"`); else the first block (`"page_top"`).

    Why a window and not a picked block: at a fixed token budget, one
    contiguous span of raw page text is far more likely to hold a
    specific sentence than one selected block. On the Bedrock anchor
    benchmark every paragraph-mode miss where the page was returned and
    the text intact sat inside a ~600-token window around the anchor or
    the page top (16 of 16), while the 80-char block floor kept
    paragraph mode from ever returning a title. The floor is untouched
    here: the window includes short blocks as neighbours, never as the
    thing selected.

    Deduplicates matches whose windows start at the same (page, lo).
    """
    from ..extractor import count_query_tokens, get_best_paragraph_for_query

    seen: dict[tuple[int, int], int] = {}
    out: list[dict[str, Any]] = []
    for m in matches:
        best_chunk = m.pop("_best_chunk", None)
        lazy_excerpt = m.pop("_lazy_excerpt", None)
        page_num_0 = m["page"] - 1
        page = _layout_page(doc, page_num_0)
        blocks = page.get_text("blocks", sort=True)
        text_blocks = [b for b in blocks if b[6] == 0]
        texts = [b[4] for b in text_blocks]
        if not texts:
            out.append(_resolve_lazy_excerpt(m, lazy_excerpt))
            continue
        anchor: int | None = None
        how = "page_top"
        if keyword_excerpts is not None and page_num_0 in keyword_excerpts:
            fragment = keyword_excerpts[page_num_0].replace("...", "").strip()
            fragment_norm = " ".join(fragment.split())
            if fragment_norm:
                for idx, bt in enumerate(texts):
                    if fragment_norm in " ".join(bt.split()):
                        anchor, how = idx, "keyword"
                        break
                if anchor is None:
                    # A snippet that spans several blocks (short page,
                    # or a window across a block join): the block it
                    # covers most.
                    cov = _block_best_covering(texts, fragment_norm)
                    if cov is not None:
                        anchor = cov
                        how = "keyword"
        if anchor is None and best_chunk:
            cov = _block_best_covering(texts, best_chunk)
            if cov is not None:
                anchor = cov
                how = "semantic"
        if anchor is None:
            _t, term_idx = get_best_paragraph_for_query(page, query)
            if term_idx is not None and count_query_tokens(texts[term_idx], query):
                anchor = term_idx
                how = "query_terms"
        if anchor is None:
            anchor, how = 0, "page_top"
        sizes = [max(1, len(t) // 4) for t in texts]
        lo, hi = _expand_block_window(sizes, anchor, window_tokens)
        window_text = "\n\n".join(t.strip() for t in texts[lo : hi + 1])
        geom: dict[str, Any] = {}
        x0 = min(b[0] for b in text_blocks[lo : hi + 1])
        y0 = min(b[1] for b in text_blocks[lo : hi + 1])
        x1 = max(b[2] for b in text_blocks[lo : hi + 1])
        y1 = max(b[3] for b in text_blocks[lo : hi + 1])
        r = page.rect
        page_rect = [round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)]
        bbox = [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]
        geom = {
            "bbox": bbox,
            "page_rect": page_rect,
            "clip": _bbox_to_clip(bbox, page_rect),
        }
        key = (m["page"], lo)
        payload = {
            **m,
            "excerpt": window_text,
            "window_blocks": [lo, hi],
            "anchor": how,
            **geom,
        }
        if key in seen:
            existing = seen[key]
            if m.get("score", 0) > out[existing].get("score", 0):
                out[existing] = payload
            continue
        seen[key] = len(out)
        out.append(payload)
    return out


_CORPUS_TERM_RE = corpus.CORPUS_TERM_RE


def _corpus_query_terms(query: str) -> set[str]:
    """Query terms used to score cross-document relevance.

    Tokens of 4+ characters only: shorter ones are function words that
    almost every document contains, so counting them would flatten the
    very signal this is computing.
    """
    return {t for t in _CORPUS_TERM_RE.findall(query.lower()) if len(t) > 3}


def _semantic_snippet_excerpt(
    path: str,
    page0: int,
    query: str,
    query_vec: Any,
    model: str,
    context_chars: int,
    chunk: str | None,
) -> str:
    """Snippet excerpt for a page matched semantically (no keyword hit
    to anchor on): the best `context_chars` span of the page's best
    sub-page chunk, or of the whole page when there is no chunk.

    `text[:context_chars]` here returned excerpts with no relation to
    the query (2026-09-01 consumer tryout, F1): on prefaced pages the
    excerpt was boilerplate while the matching content sat further
    down. Lexical rescue (p34, second form): when the best-scoring
    chunk holds no literal query term but the page does, widen the
    span search to the whole page, otherwise the term windows the
    back-off in `_best_span_in_text` needs are outside the searched
    text. Shared by every semantic-only hit in both search tools and
    every mode, so the rule lives in one place.
    """
    page_text = _core.cache.get_page_text(path, page0) or ""
    text = chunk or page_text
    if chunk and page_text:
        terms = _corpus_query_terms(query)
        low_chunk = chunk.lower()
        if terms and not any(t in low_chunk for t in terms):
            if any(t in page_text.lower() for t in terms):
                text = page_text
    span = _best_span_in_text(text, query_vec, model, context_chars, query=query)
    return _whole_token_span(span, page_text, text)


def _whole_token_span(span: str, page_text: str, searched: str) -> str:
    """Widen a semantic span to whole tokens and mark its cuts, the same
    shape keyword excerpts have. The span is a raw character window, so it
    ended mid-word and carried no "..." markers. Markers are judged against
    the page text where the span can be found there, because a span that
    opens a sub-page chunk does not open the page. Fail-safe: a span found
    in neither text comes back unchanged."""
    from ..extractor import widen_to_token_bounds

    if not span.strip():
        return span
    for source in (page_text, searched):
        if source:
            i = source.find(span)
            if i >= 0:
                return widen_to_token_bounds(source, i, i + len(span))
    return span


def _best_subchunk_text(
    path: str, page_num_0: int, vecs_by_page: dict[int, Any], query_vec: Any
) -> str | None:
    """Text of the page's best-scoring sub-page embedding unit (unit 0 is
    the whole page and is skipped), or None when the page has a single
    unit or no cached vectors."""
    from ..extractor import page_embedding_units

    vecs = vecs_by_page.get(page_num_0)
    if vecs is None or len(vecs) < 2:
        return None
    text = _core.cache.get_page_text(path, page_num_0) or ""
    units = page_embedding_units(text)
    if len(units) != len(vecs):
        return None
    scores = [float(v @ query_vec) for v in vecs[1:]]
    return units[1 + max(range(len(scores)), key=scores.__getitem__)]
