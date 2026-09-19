"""table_context helpers for pdf_search."""

import logging
import re
from typing import Any
from ..cache import PDFCache
from ..extractor import _NUMBER_TOKEN, _columns_reliable, extract_tables_for_pages
from ._render import _bbox_to_clip

logger = logging.getLogger(__name__)


#: Words that give a number its column identity. Kept separate from the
#: benchmark's own copy on purpose: this decides when to spend a
#: subprocess, the benchmark decides whether an answer is resolvable.
#: Sharing them would move the fix and its ruler together.
_COLUMN_IDENTITY_WORDS = re.compile(
    r"\b(min|max|typ|typical|minimum|maximum|value|rating)\b", re.I
)
#: Thousands separators are part of the number. Without them "4,350.4"
#: reads as two tokens, which made a clean financial cell look merged to
#: `_columns_reliable` and a single-value excerpt look ambiguous to
#: `_excerpt_is_ambiguous`, on every table that groups thousands.
#: A currency symbol or a thousands-grouped amount. Marks a cell as data
#: rather than a column label, which is what keeps a sparse-but-real header
#: from being displaced by the row beneath it.
_MONEY_CELL = re.compile(r"[$€£¥]|\d,\d{3}")
#: Upper bound on rows returned in a table_context, so a match covering a
#: whole large table cannot bloat the response.
_MAX_CONTEXT_ROWS = 20


def _excerpt_is_ambiguous(excerpt: str) -> bool:
    """True when a caller cannot tell which quantity a number is.

    Two or more numbers with nothing naming the columns: the header lives
    in a different text block, and position is no substitute because empty
    cells are elided. Anything else needs no table context, so a prose or
    single-value search spawns no subprocess.
    """
    if _COLUMN_IDENTITY_WORDS.search(excerpt):
        return False
    return len(_NUMBER_TOKEN.findall(excerpt)) >= 2


#: Matches a reference to a numbered table, which is what a caption is.
_TABLE_REF = re.compile(r"\btable\s+\d", re.I)


def _match_may_touch_a_table(excerpt: str) -> bool:
    """Cheap pre-filter: is this worth EXTRACTING tables for?

    Decided from the excerpt alone, before any subprocess, because the
    geometric test needs tables and extracting them for every matched
    page would make prose searches pay for a spawn. Two routes, both
    from measured failure buckets: an ambiguous value list, or a caption
    naming a table.

    A third TEXTUAL route was tried and removed: "short block carrying no
    value", aimed at bare row labels like 'Timing Error, Monostable'.
    Nothing textual separates those from short prose -- it fired on "no
    numbers here at all". Those labels are now reached by geometry
    instead, via `_block_is_ruled`, which reads the drawn rules around the
    block rather than the words inside it.
    """
    if _excerpt_is_ambiguous(excerpt):
        return True
    return bool(_TABLE_REF.search(excerpt))


#: How far from a block edge a drawn rule can sit and still be read as that
#: block's own rule. Measured on the graded corpus, the banded test is clean
#: from 20pt through 90pt -- distance barely discriminates, because what
#: separates a table row from prose is that horizontally-spanning rules exist
#: on BOTH sides at all. 30pt is roughly two lines, chosen mid-plateau.
_RULE_BAND = 30.0
#: A rule must span this fraction of the block to be the block's own rule
#: rather than an unrelated segment at the same height. The graded corpus
#: does not exercise this floor (0.3 and 0.7 score identically); it is a
#: guard, not a tuned parameter.
_RULE_X_OVERLAP = 0.5


def _page_rules(page: Any) -> list[tuple[float, float, float]]:
    """Horizontal drawn rules on a page, as (x0, x1, y).

    Reads drawing commands only: no text, no `find_tables`, and no
    subprocess, so this stays admissible inside a prose search.

    Length-agnostic on purpose. Financial statements rule each numeric
    column separately -- Berkshire 2024 p67 draws 36 rules of 45.6pt -- so
    any absolute length floor calibrated on datasheet tables would miss
    them entirely.
    """
    out: list[tuple[float, float, float]] = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            if item[0] == "l":
                p0, p1 = item[1], item[2]
                if abs(p1.y - p0.y) <= 1.5 and abs(p1.x - p0.x) >= 8:
                    out.append((min(p0.x, p1.x), max(p0.x, p1.x), (p0.y + p1.y) / 2))
            elif item[0] == "re":
                rect = item[1]
                # A thin filled rectangle is how many PDFs draw a rule.
                if rect.height <= 3 and rect.width >= 8:
                    out.append((rect.x0, rect.x1, (rect.y0 + rect.y1) / 2))
    return out


def _block_is_ruled(bbox: list[Any], rules: list[tuple[float, float, float]]) -> bool:
    """True when a block sits in a ruled band, i.e. it is a table row.

    The route that reaches a bare row label. 'Thermal Shutdown Protection'
    holds no number and names no table, so every textual test is blind to
    it; what marks it as tabular is that rules spanning its width run both
    above and below it.

    Banded rather than "a rule within N points of the edge": a wrapped
    label occupies only part of a tall row, so its own rules can be 3pt
    above and 18pt below. Requiring a rule near each EDGE missed exactly
    that shape.
    """
    x0, y0, x1, y1 = (float(v) for v in bbox)
    width = max(x1 - x0, 1.0)
    above = below = False
    for rx0, rx1, ry in rules:
        if (min(x1, rx1) - max(x0, rx0)) / width < _RULE_X_OVERLAP:
            continue
        if y0 - _RULE_BAND <= ry <= y0 + 2:
            above = True
        if y1 - 2 <= ry <= y1 + _RULE_BAND:
            below = True
        if above and below:
            return True
    return False


#: How far above or below a table a block can sit and still be read as
#: belonging to it (a caption, a note). 60pt is roughly four lines.
#: Captions are the single largest retrieval-failure bucket: 7 of 19.
_TABLE_ASSOCIATION_GAP = 60.0


def _table_near_match(bbox: list[Any], table: dict[str, Any]) -> bool:
    """True when a match block sits inside, or just beside, a table.

    Geometric on purpose. The answer lives in a VALUE block scoring 0-4
    query tokens while the picker's winner is a label, caption or prose
    block scoring 3-6, so no token-overlap scorer can prefer the value
    block. Three re-ranking designs died on that. Position can tell what
    scoring cannot: the caption of a table belongs to that table.
    """
    tb = table.get("bbox")
    if not tb:
        return False
    y0, y1 = float(bbox[1]), float(bbox[3])
    t0, t1 = float(tb[1]), float(tb[3])
    if y1 >= t0 - 1 and y0 <= t1 + 1:
        return True  # overlapping or inside
    if 0 <= t0 - y1 <= _TABLE_ASSOCIATION_GAP:
        return True  # sits above, like a caption
    return 0 <= y0 - t1 <= _TABLE_ASSOCIATION_GAP  # sits below, like a note


def _excerpt_wants_table_context(excerpt: str, near_table: bool) -> bool:
    """Should this match carry table context?

    Two routes. The original: the excerpt holds several numbers and no
    column label, so the caller cannot tell which quantity is which. The
    second: the excerpt sits in or beside a table, which covers the case
    the first cannot see at all -- a caption or row label carrying NO
    value, where the excerpt is not ambiguous, it is simply incomplete.
    """
    if _excerpt_is_ambiguous(excerpt):
        return True
    return near_table


def _resolve_header(
    header: list[str], rows: list[list[str]]
) -> tuple[list[str], list[list[str]]]:
    """Return the real column header and the remaining body rows.

    PyMuPDF sometimes reports a caption or section title as the header
    (Diodes p2 yields the 'Electrical Characteristics (@ TA = ...)' banner)
    while the real column labels sit in row 0. Promote row 0 only when it is
    clearly more header-like, so a genuine header is never discarded.

    Two independent signals, because either alone misses real tables:

    - vocabulary: row 0 names min/typ/max and the header does not. Catches
      datasheets, and is what shipped first.
    - structure: a caption occupies ONE cell and leaves the rest empty,
      whereas a header row fills several. This is vocabulary-free, which
      matters because real column labels are arbitrary noun phrases.
      Measured on three document families, only one of which the
      vocabulary rule caught: the Federal Reserve consumer report's
      'Rate advertised on website | Product details | Estimated APR
      equivalent', Berkshire 2024 p134's fiscal-year row, and Vishay's
      'Characteristic | Min | Max'.

    The structural rule needs 3+ columns. At two columns, "one filled
    header cell, two filled row cells" is the shape of ordinary data and
    cannot be told from a caption.
    """
    if not rows:
        return header, rows

    def col_words(cells: list[str]) -> int:
        return sum(1 for c in cells if c and _COLUMN_IDENTITY_WORDS.search(c))

    def filled(cells: list[str]) -> int:
        return sum(1 for c in cells if c and c.strip())

    # A header names columns; it does not carry money. Berkshire p55 has a
    # real year header spread thinly across 12 columns, and without this
    # the sparse-caption allowance below promoted its data row ('$',
    # '9,020') over a correct header.
    if any(_MONEY_CELL.search(c) for c in rows[0] if c):
        return header, rows

    by_vocabulary = col_words(header) < 2 and col_words(rows[0]) >= 2
    # Sparse caption band, denser row beneath it. The quarter-of-columns
    # allowance covers side-by-side sub-tables merged into one detection
    # (Berkshire p134 is 20 columns with two captions), while `max(1, ...)`
    # keeps narrow tables at the single-cell reading.
    #
    # The `>= 2` floor is load-bearing when the real header is entirely
    # empty, which happens when `find_tables` cuts the column-label row out
    # of the detected bbox (Starbucks p36: the fiscal-year dates sit above
    # the table). Then `2 * filled(header)` is zero and ANY non-empty row 0
    # would promote, so a lone section label ('Net revenues:') became the
    # header. A header names two or more columns; one filled cell is a
    # section band, not a header.
    by_structure = (
        len(header) >= 3
        and filled(header) <= max(1, len(header) // 4)
        and filled(rows[0]) >= 2 * filled(header)
        and filled(rows[0]) >= 2
    )
    if by_vocabulary or by_structure:
        return rows[0], rows[1:]
    return header, rows


def _attach_table_context(
    matches: list[dict[str, Any]],
    local_path: str,
    cache: PDFCache,
    doc: Any | None = None,
) -> list[dict[str, Any]]:
    """Attach header + matched row to ambiguous matches, in place of nothing.

    Only ambiguous matches carrying a bbox are candidates, so prose
    searches spawn nothing. All candidate pages for this document are
    served by ONE isolated worker call and persisted in page_tables, so a
    page costs extraction once.

    Extraction must stay out of process: this interpreter has imported
    pymupdf4llm, which corrupts find_tables irreversibly.
    """
    placed = [m for m in matches if m.get("bbox")]
    if not placed:
        return matches

    # Reading the cache is free, so do it for EVERY matched page. Only
    # pages whose excerpt passes the pre-filter are worth an extraction.
    # That separation is what lets a bare row label ('Timing Error,
    # Monostable') associate with its table without prose searches ever
    # paying for a spawn: no cached tables, no attachment, no cost.
    tables_by_page: dict[int, list[dict[str, Any]]] = {}
    for page_num in sorted({m["page"] - 1 for m in placed}):
        cached = cache.get_page_tables(local_path, page_num)
        if cached is not None:
            tables_by_page[page_num] = cached

    # The textual routes first, because they cost nothing. Only a match they
    # reject is worth reading page geometry for, so a prose search pays the
    # drawing scan on pages it was going to skip anyway, and never on a page
    # whose tables are already cached.
    candidates = []
    rules_by_page: dict[int, list[tuple[float, float, float]]] = {}
    for m in placed:
        if _match_may_touch_a_table(m.get("excerpt", "")):
            candidates.append(m)
            continue
        page_num = m["page"] - 1
        if doc is None or page_num in tables_by_page:
            continue
        if page_num not in rules_by_page:
            try:
                rules_by_page[page_num] = _page_rules(doc[page_num])
            except (IndexError, ValueError, RuntimeError):
                rules_by_page[page_num] = []
        if _block_is_ruled(m["bbox"], rules_by_page[page_num]):
            candidates.append(m)

    missing = sorted(
        {m["page"] - 1 for m in candidates if (m["page"] - 1) not in tables_by_page}
    )

    if missing:
        try:
            out = extract_tables_for_pages(local_path, missing).get("tables", {})
        except Exception as exc:  # noqa: BLE001 - table context is optional
            logger.warning("Table context extraction failed: %s", exc)
            out = {}
        for page_num in missing:
            extracted = out.get(str(page_num))
            if isinstance(extracted, list):
                tables_by_page[page_num] = extracted
                cache.save_page_tables(local_path, page_num, extracted)

    for m in placed:
        page_rect = None
        if doc is not None:
            try:
                page_rect = [round(v, 1) for v in doc[m["page"] - 1].rect]
            except (IndexError, ValueError, RuntimeError):
                page_rect = None
        page_tables = tables_by_page.get(m["page"] - 1, [])
        near = any(_table_near_match(m["bbox"], t) for t in page_tables)
        if not _excerpt_wants_table_context(m.get("excerpt", ""), near):
            continue
        ctx = _context_for_match(m, page_tables, page_rect)
        if ctx is not None:
            m["table_context"] = ctx
    return matches


def _context_for_match(
    match: dict[str, Any],
    tables: list[dict[str, Any]],
    page_rect: list[float] | None = None,
) -> dict[str, Any] | None:
    """Header and every table row the match bbox covers.

    Geometric, not token overlap: token overlap was measured selecting the
    wrong row (5 vs 3 on the right one).

    Returns ALL covered rows rather than choosing one. An excerpt block
    routinely spans several rows (Starbucks p34 covers six, Berkshire p134
    the whole table), and nothing in the geometry says which of them the
    caller wants. Picking one returned the wrong row; insisting on exactly
    one returned nothing at all on every document whose blocks are not
    per-row. The rows are already in the excerpt the caller can see, so the
    part actually missing is the header, and handing back the header with
    the rows it governs resolves the ambiguity without a guess.
    """
    bbox = match["bbox"]
    for table in tables:
        row_bboxes = table.get("row_bboxes") or []
        rows = table.get("rows") or []
        if len(row_bboxes) != len(rows):
            continue
        header, body = _resolve_header(table.get("header") or [], rows)
        offset = len(rows) - len(body)  # 1 when row 0 was promoted
        hits = _rows_overlapping(bbox, row_bboxes[offset:])
        if hits:
            reliable = _columns_reliable(body)
            ctx: dict[str, Any] = {
                "header": header,
                "rows": [body[i] for i in hits[:_MAX_CONTEXT_ROWS]],
                "columns_reliable": reliable,
            }
            # Columns unreadable in TEXT are still legible on the PAGE:
            # TI LM555 draws MIN and MAX in separate visual columns while
            # both collapse into one cell as "4.5 16". Hand back the
            # region to render rather than leaving the caller with a
            # value it cannot attribute. Same call `pdf_extract_chart`
            # makes when it declines and returns a render.
            tb = table.get("bbox")
            if not reliable and tb and page_rect:
                ctx["bbox"] = list(tb)
                ctx["clip"] = _bbox_to_clip(tb, page_rect)
            return ctx

    # No row contains the match. A caption or note sits BESIDE its table
    # rather than in it, and that is the largest failure bucket: the
    # picker lands on "Table 3: Variations on the Transformer
    # architecture" while every value sits in the table below. Hand back
    # the associated table whole.
    for table in tables:
        if not _table_near_match(bbox, table):
            continue
        rows = table.get("rows") or []
        if not rows:
            continue
        header, body = _resolve_header(table.get("header") or [], rows)
        if not body:
            continue
        near_ctx: dict[str, Any] = {
            "header": header,
            "rows": body[:_MAX_CONTEXT_ROWS],
            "columns_reliable": _columns_reliable(body),
        }
        tb = table.get("bbox")
        if tb and page_rect:
            near_ctx["bbox"] = list(tb)
            near_ctx["clip"] = _bbox_to_clip(tb, page_rect)
        return near_ctx
    return None


def _rows_overlapping(bbox: list[Any], row_bboxes: list[list[float]]) -> list[int]:
    """Indices of rows the bbox overlaps by more than half the smaller height.

    Scaled by ``min(bbox height, row height)`` so it works in both
    directions: a short cell bbox sitting inside a tall row still counts as
    one hit, while a tall whole-table bbox counts every row it covers and so
    resolves to no single row.
    """
    y0, y1 = float(bbox[1]), float(bbox[3])
    box_h = y1 - y0
    hits: list[int] = []
    for i, rb in enumerate(row_bboxes):
        r0, r1 = float(rb[1]), float(rb[3])
        row_h = r1 - r0
        if row_h <= 0:
            continue
        overlap = min(y1, r1) - max(y0, r0)
        if overlap > 0.5 * min(box_h, row_h):
            hits.append(i)
    return hits
