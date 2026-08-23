"""hybrid get_drawings() - pdfium object API by default, raw
content-stream parsing only where pdfium structurally cannot see.

Measured motivation (see pypdfium2-spike-results.md sections 9-10), both
arms scored against the real PyMuPDF get_drawings() on the same corpora:

| arm | synthetic 33 | real-world 136 |
| --- | --- | --- |
| pdfium object API  | 0 wrong-emit | 8 wrong-emit |
| content-stream parser | 2 wrong-emit | 17 wrong-emit |

Neither dominates. The pdfium arm is better almost everywhere, but has one
*structural* blind spot it can never fix: vector content painted via
Tiling Patterns (`/Pattern cs /pN scn`), which pdfium exposes no API for
at all - on one real page it recovered 0.7% of the geometry. The parser
arm sees that content but is otherwise noisier.

So: route per page. Use pdfium unless the page actually uses a tiling
pattern, in which case the parser is the only arm that can see the data.
"""

from __future__ import annotations

from typing import Any

import pypdfium2 as pdfium
from pypdf import PdfReader
from pypdf.generic import DictionaryObject

from .content_stream import get_drawings as parse_drawings
from .drawings import get_drawings as pdfium_drawings

_MAX_DEPTH = 6


def _resources_use_pattern(res: DictionaryObject | None, depth: int = 0) -> bool:
    """True if this resource dict, or any Form XObject reachable from it,
    declares a /Pattern. Cheap dictionary walk - no stream decoding."""
    if res is None or depth > _MAX_DEPTH:
        return False
    try:
        pats = res.get("/Pattern")
        if pats is not None and len(pats.get_object()) > 0:
            return True
        xobjs = res.get("/XObject")
        if xobjs is None:
            return False
        for xo in xobjs.get_object().values():
            xo = xo.get_object()
            if str(xo.get("/Subtype")) != "/Form":
                continue
            sub = xo.get("/Resources")
            if sub is not None and _resources_use_pattern(sub.get_object(), depth + 1):
                return True
    except Exception:  # noqa: BLE001 - a malformed resource tree just means "no"
        return False
    return False


def page_uses_tiling_pattern(pdf_path: str, page_num: int) -> bool:
    try:
        page = PdfReader(pdf_path).pages[page_num]
        res = page.get("/Resources")
        return _resources_use_pattern(res.get_object() if res else None)
    except Exception:  # noqa: BLE001
        return False


def get_drawings(
    pdf_path: str, page_num: int, fpx_page: pdfium.PdfPage | None = None
) -> list[dict[str, Any]]:
    """Route to whichever arm can actually see this page's vector content."""
    if page_uses_tiling_pattern(pdf_path, page_num):
        return parse_drawings(pdf_path, page_num)
    if fpx_page is None:
        doc = pdfium.PdfDocument(pdf_path)
        return pdfium_drawings(doc[page_num])
    return pdfium_drawings(fpx_page)


__all__ = ["get_drawings", "page_uses_tiling_pattern"]
