"""Attach `section_path` and `lead_in` to page-granularity search hits."""

import logging
import os
from collections import OrderedDict
from typing import Any

from ..hit_context import OutlineEntry, build_outline, lead_in, section_path
from ._search_common import _layout_page

logger = logging.getLogger(__name__)

#: Outlines memoised per (path, mtime): a corpus query touches each
#: document once per hit, and reading an outline costs up to ~30 ms on a
#: large annual report.
_OUTLINE_MEMO: "OrderedDict[tuple[str, float], list[OutlineEntry]]" = OrderedDict()
_OUTLINE_MEMO_MAX = 256


def _outline_for(path: str, doc: Any) -> list[OutlineEntry]:
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return []
    memo = _OUTLINE_MEMO.get(key)
    if memo is not None:
        _OUTLINE_MEMO.move_to_end(key)
        return memo
    outline = build_outline(doc.get_toc(), len(doc))
    _OUTLINE_MEMO[key] = outline
    if len(_OUTLINE_MEMO) > _OUTLINE_MEMO_MAX:
        _OUTLINE_MEMO.popitem(last=False)
    return outline


def attach_hit_context(
    hits: list[dict[str, Any]], path: str, doc: Any
) -> list[dict[str, Any]]:
    """Set `section_path` / `lead_in` on each hit where they apply.

    Both fields are absent, never null, when they do not apply. Best-effort:
    a failure on one hit leaves that hit as it was.
    """
    try:
        outline = _outline_for(path, doc)
        page_count = len(doc)
    except Exception:  # noqa: BLE001 - context is optional
        logger.debug("outline unavailable for %s", path, exc_info=True)
        outline, page_count = [], 0
    for hit in hits:
        try:
            blocks = [
                b
                for b in _layout_page(doc, hit["page"] - 1).get_text(
                    "blocks", sort=True
                )
                if str(b[4]).strip()
            ]
            bbox = hit.get("bbox")
            hit_top = float(bbox[1]) if bbox else None
            if outline:
                path_ = section_path(outline, page_count, hit["page"], hit_top, blocks)
                if path_:
                    hit["section_path"] = path_
            intro = lead_in(blocks, bbox, hit.get("excerpt", ""))
            if intro:
                hit["lead_in"] = intro
        except Exception:  # noqa: BLE001
            logger.debug("hit context failed on %s p%s", path, hit.get("page"))
    return hits
