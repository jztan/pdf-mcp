"""Where a search hit sits: its outline section path and the table lead-in.

Pure logic, no MCP dependency. A table row in an excerpt can be read in the
wrong frame: a segment page looks like the consolidated statement, a pro
forma table like actual results (issue #66). Two signals the page already
carries tell them apart:

- `section_path`: the PDF's own outline entries enclosing the hit. Outline
  only: heuristic headings were measured unusable as labels (msft-fy2024:
  1,428 detected "sections", 1,129 of them digits or table cells).
- `lead_in`: the sentence printed above a table that introduces it, ending
  in a colon ("Following are ... on an unaudited pro forma basis ...:").
  Structural, no vocabulary: word lists do not converge.

Both are best-effort context, absent rather than guessed when unsure.
"""

import re
from dataclasses import dataclass
from typing import Any, Sequence

#: An outline entry whose range runs to the end of the document only
#: because nothing follows it covers at most this many pages past its
#: start. Without the cap, back matter the outline does not list
#: (References, Contributions) inherited the last entry: every wrong
#: section path in the first 100 read by eye.
_OPEN_TAIL_PAGES = 2
#: An entry spanning this share of the document names nothing (a report
#: title over every page).
_WHOLE_DOC_SHARE = 0.9
_MAX_PATH_LEVELS = 3
_TITLE_MAX_CHARS = 100
#: Heading lookup compares this many normalised characters of the title.
_TITLE_KEY_CHARS = 40

_LEAD_MIN_CHARS = 40
_LEAD_MAX_CHARS = 400
#: Furniture blocks (unit bands, year headers) the walk may cross.
_MAX_FURNITURE = 4
_FURNITURE_MAX_CHARS = 120
_FURNITURE_MAX_WORDS = 8
_PROSE_MIN_WORDS = 12
#: A block ending in `.` or `:` with this many words is a sentence.
_SENTENCE_MIN_WORDS = 6

_NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_WORD = re.compile(r"[A-Za-z]{3,}")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_DATE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    re.I,
)
_FOOTNOTE = re.compile(r"^\(?(?:\d{1,2}|[a-z])\)\s")
_BULLET = re.compile(r"^[•●▪◦*\-–]")


@dataclass(frozen=True)
class OutlineEntry:
    level: int
    title: str
    start: int  # 1-indexed
    end: int  # 1-indexed, inclusive, >= start
    #: Last page the entry can reach: the page its successor's heading is
    #: printed on, since text above that heading still belongs here.
    reach: int


def build_outline(toc: Sequence[Sequence[Any]], page_count: int) -> list[OutlineEntry]:
    """Outline entries with page ranges, from ``[level, title, page]`` rows.

    Rows with no usable page or an empty title are dropped. An entry ends
    before the next one at the same or a higher level; one that nothing
    follows is capped at ``_OPEN_TAIL_PAGES`` past its start.
    """
    rows = [
        (int(r[0]), " ".join(str(r[1]).split()), int(r[2]))
        for r in toc
        if len(r) >= 3 and str(r[1]).strip() and 1 <= int(r[2]) <= page_count
    ]
    out: list[OutlineEntry] = []
    for i, (level, title, start) in enumerate(rows):
        nxt = next((r[2] for r in rows[i + 1 :] if r[0] <= level), None)
        if nxt is None:
            end = reach = min(page_count, start + _OPEN_TAIL_PAGES)
        else:
            end, reach = max(start, nxt - 1), max(start, nxt)
        out.append(OutlineEntry(level, title, start, end, reach))
    return out


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def _clip(title: str) -> str:
    if len(title) <= _TITLE_MAX_CHARS:
        return title
    return title[:_TITLE_MAX_CHARS].rsplit(" ", 1)[0] + "..."


def _heading_top(title: str, blocks: Sequence[Sequence[Any]]) -> float | None:
    key = _norm(title)[:_TITLE_KEY_CHARS]
    if not key:
        return None
    for b in blocks:
        if _norm(str(b[4])).startswith(key):
            return float(b[1])
    return None


def section_path(
    outline: Sequence[OutlineEntry],
    page_count: int,
    page: int,
    hit_top: float | None,
    blocks: Sequence[Sequence[Any]],
) -> list[str] | None:
    """Outline titles enclosing a hit, root to leaf, at most 3; else None.

    ``page`` is 1-indexed; ``hit_top`` is the hit's top y (None when the hit
    has no geometry); ``blocks`` are the page's text blocks. Outline order
    does not follow page order (JPM FY2023 p116 lists a table heading before
    the prose heading printed above it), so among entries that start on the
    hit's page the heading printed nearest above the hit wins. A heading on
    the page closes deeper sections carried over from earlier pages.
    """
    carried: dict[int, str] = {}  # started on an earlier page
    on_page: dict[int, tuple[float, str]] = {}  # started here, above the hit
    starts_here: dict[int, int] = {}  # entries per level starting here
    for e in outline:
        if (e.end - e.start + 1) >= _WHOLE_DOC_SHARE * page_count:
            continue
        if e.start < page <= e.reach:
            carried[e.level] = e.title
        elif e.start == page:
            top = _heading_top(e.title, blocks)
            if top is None:
                # Not locatable on the page: fall back to the page-level
                # reading, as if it were printed at the top.
                top = float("-inf")
            elif hit_top is not None and top > hit_top + 1.0:
                continue  # printed below the hit: not started yet
            if e.level not in on_page or top >= on_page[e.level][0]:
                on_page[e.level] = (top, e.title)
            starts_here[e.level] = starts_here.get(e.level, 0) + 1
    if hit_top is None:
        # No geometry: with two headings of one level on the page, which
        # one governs the hit is unknown. Stop above that level.
        ambiguous = [lvl for lvl, n in starts_here.items() if n > 1]
        if ambiguous:
            cut = min(ambiguous)
            carried = {lvl: t for lvl, t in carried.items() if lvl < cut}
            on_page = {lvl: v for lvl, v in on_page.items() if lvl < cut}
    # A deeper heading printed before a shallower one on this page was
    # closed by it (FlashAttention p22: B.5 then C Proofs; a hit below both
    # is in C, not C > B.5).
    floor = float("-inf")
    for lvl in sorted(on_page):
        top, _title = on_page[lvl]
        if top < floor:
            on_page = {k: v for k, v in on_page.items() if k < lvl}
            break
        floor = top
    if on_page:
        shallowest = min(on_page)
        carried = {lvl: t for lvl, t in carried.items() if lvl < shallowest}
        carried.update({lvl: t for lvl, (_top, t) in on_page.items()})
    if not carried:
        return None
    path = [_clip(carried[lvl]) for lvl in sorted(carried)]
    return path[-_MAX_PATH_LEVELS:]


def _flat(text: Any) -> str:
    return " ".join(str(text).split())


def _is_furniture(text: str) -> bool:
    """A unit band or period header: short, few words, no data value."""
    if len(text) > _FURNITURE_MAX_CHARS:
        return False
    undated = _DATE.sub(" ", text)
    if any(not _YEAR.fullmatch(n) for n in _NUMBER.findall(undated)):
        return False  # a data value: crossing it would cross a table
    words = len(_WORD.findall(text))
    if text.endswith((".", ":")) and words >= _SENTENCE_MIN_WORDS:
        return False  # a short sentence, possibly the lead-in itself
    return words <= _FURNITURE_MAX_WORDS


def _is_prose(text: str) -> bool:
    if _BULLET.match(text) or _FOOTNOTE.match(text):
        return False
    if len(_WORD.findall(text)) < _PROSE_MIN_WORDS:
        return False
    return text.endswith(".") or ". " in text


def _overlap(block: Sequence[Any], bbox: Sequence[float]) -> float:
    x0, y0, x1, y1 = (float(v) for v in block[:4])
    w = min(x1, bbox[2]) - max(x0, bbox[0])
    h = min(y1, bbox[3]) - max(y0, bbox[1])
    return max(0.0, w) * max(0.0, h)


def lead_in(
    blocks: Sequence[Sequence[Any]],
    hit_bbox: Sequence[float] | None,
    excerpt: str,
) -> str | None:
    """The colon sentence introducing the hit's block, or None.

    Walks up from the block the hit overlaps most, crossing at most
    ``_MAX_FURNITURE`` unit or period bands. A prose hit gets none.
    """
    if not hit_bbox or not blocks:
        return None
    idx = max(range(len(blocks)), key=lambda i: _overlap(blocks[i], hit_bbox))
    if _overlap(blocks[idx], hit_bbox) <= 0.0:
        return None
    if _is_prose(_flat(blocks[idx][4])):
        return None
    crossed = 0
    for j in range(idx - 1, -1, -1):
        text = _flat(blocks[j][4])
        if not text:
            continue
        if _is_furniture(text):
            crossed += 1
            if crossed > _MAX_FURNITURE:
                return None
            continue
        if (
            text.endswith(":")
            and _LEAD_MIN_CHARS <= len(text) <= _LEAD_MAX_CHARS
            and not _FOOTNOTE.match(text)
            and not _BULLET.match(text)
            and not text.startswith("_")
            and _norm(text) not in _norm(excerpt)
        ):
            return text
        return None
    return None
