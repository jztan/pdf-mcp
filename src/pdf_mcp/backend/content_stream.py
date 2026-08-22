"""raw PDF content-stream parser for the path-drawing subset.

Exists because of the gap found in section 9 of pypdfium2-spike-results.md:
some PDFs paint chart geometry inside **Tiling Patterns used as fill
colors** (`/Pattern cs /pN scn` + `re f`). PDFium's page-object API cannot
reach that content - a pattern is referenced as a *colour*, not drawn as
an object, so neither FPDFPage_CountObjects nor FPDFFormObj_CountObjects
descends into it, and `pypdfium2.raw` exposes no pattern/tiling functions
at all.

This parser goes around that by reading the content streams directly and
tokenising PDF operators itself. Object/stream access uses **pypdf**
(BSD-3-Clause), so the whole stack stays permissive: pypdfium2 +
pdfplumber + pypdf.

Scope: the path-construction/painting subset that chart_extractor.py
consumes - q/Q/cm/gs, m/l/c/v/y/re/h, S/s/f/F/f*/B/B*/b/b*/n, w, d,
RG/rg/G/g/K/k, Do (recurse into Form XObjects), and cs/scn (recurse into
Tiling Patterns). Text (BT..ET) is skipped; shadings are not handled.
"""

from __future__ import annotations

import re
from typing import Any

from pypdf import PdfReader
from pypdf.generic import ContentStream, DictionaryObject, NameObject

from .geometry import Point as _Pt
from .geometry import Quad as _Quad
from .geometry import Rect as _Rect

Matrix = tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_MAX_DEPTH = 8  # guard against pathological/recursive resource graphs


def _mul(m1: Matrix, m2: Matrix) -> Matrix:
    """m1 then m2 (i.e. m1 x m2 in PDF's row-vector convention)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _scale_of(m: Matrix) -> float:
    a, b, c, d, _, _ = m
    return float(abs(a * d - b * c) ** 0.5)


class _GState:
    __slots__ = (
        "ctm",
        "stroke_color",
        "fill_color",
        "line_width",
        "dashes",
        "fill_pattern",
    )

    def __init__(self) -> None:
        self.ctm: Matrix = _IDENTITY
        self.stroke_color: tuple[float, float, float] | None = None
        self.fill_color: tuple[float, float, float] | None = None
        self.line_width: float = 1.0
        self.dashes: str = "[] 0"
        self.fill_pattern: str | None = None

    def copy(self) -> "_GState":
        g = _GState()
        g.ctm = self.ctm
        g.stroke_color = self.stroke_color
        g.fill_color = self.fill_color
        g.line_width = self.line_width
        g.dashes = self.dashes
        g.fill_pattern = self.fill_pattern
        return g


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class ContentWalker:
    """Walks a page's content streams (recursing into Form XObjects and
    Tiling Patterns) and emits PyMuPDF-`get_drawings()`-shaped dicts."""

    def __init__(self, page_height: float, x_offset: float = 0.0) -> None:
        self.page_height = page_height
        self.x_offset = x_offset
        self.out: list[dict[str, Any]] = []

    # -- geometry helpers -------------------------------------------------

    def _pt(self, m: Matrix, x: float, y: float) -> _Pt:
        px, py = _apply(m, x, y)
        return _Pt(round(px - self.x_offset, 2), round(self.page_height - py, 2))

    # -- main walk --------------------------------------------------------

    def walk(
        self,
        data: bytes,
        resources: DictionaryObject | None,
        base_ctm: Matrix,
        depth: int = 0,
    ) -> None:
        if depth > _MAX_DEPTH:
            return
        try:
            cs = ContentStream(_RawStream(data), None)
        except Exception:  # noqa: BLE001 - malformed stream: skip, don't crash
            return

        gs = _GState()
        gs.ctm = base_ctm
        stack: list[_GState] = []
        # Path state: a list of subpaths, each accumulating both its raw
        # segments and its vertex list. Normalisation to 're'/'qu' happens
        # at paint time, because a rectangle may arrive either as the `re`
        # operator or as m/l/l/l/h - matplotlib emits the latter, and
        # PyMuPDF normalises both to a single 're' verb. chart_extractor's
        # rects_of()/_hrule_bars() key on that verb, so we must match.
        subpaths: list[dict[str, Any]] = []
        pos: Any = None
        in_text = False

        def new_subpath(p: Any) -> None:
            subpaths.append({"verts": [p], "segs": [], "closed": False, "rect": None})

        def add_seg(seg: tuple[Any, ...], end: Any) -> None:
            if not subpaths:
                new_subpath(seg[1])
            subpaths[-1]["segs"].append(seg)
            subpaths[-1]["verts"].append(end)

        def normalize() -> list[tuple[Any, ...]]:
            """Subpaths -> PyMuPDF-shaped items, with rect/quad detection."""
            items: list[tuple[Any, ...]] = []
            for sp in subpaths:
                if sp["rect"] is not None:
                    items.append(("re", sp["rect"], 0))
                    continue
                verts = sp["verts"]
                uniq = verts[:-1] if len(verts) > 1 and verts[0] == verts[-1] else verts
                closed = sp["closed"] or (len(verts) > 2 and verts[0] == verts[-1])
                if closed and len(uniq) == 4 and all(s[0] == "l" for s in sp["segs"]):
                    xs = sorted({p.x for p in uniq})
                    ys = sorted({p.y for p in uniq})
                    if len(xs) == 2 and len(ys) == 2:
                        items.append(("re", _Rect(xs[0], ys[0], xs[1], ys[1]), 0))
                        continue
                    by_y = sorted(uniq, key=lambda p: p.y)
                    top = sorted(by_y[:2], key=lambda p: p.x)
                    bot = sorted(by_y[2:], key=lambda p: p.x)
                    items.append(("qu", _Quad(top[0], top[1], bot[0], bot[1])))
                    continue
                items.extend(sp["segs"])
            return items

        def flush(paint: str) -> None:
            items = normalize()
            if items:
                has_stroke = paint in ("S", "s", "B", "B*", "b", "b*")
                has_fill = paint in ("f", "F", "f*", "B", "B*", "b", "b*")
                if has_stroke or has_fill:
                    self.out.append(
                        {
                            "type": (
                                "fs"
                                if (has_fill and has_stroke)
                                else ("f" if has_fill else "s")
                            ),
                            "color": gs.stroke_color if has_stroke else None,
                            "fill": gs.fill_color if has_fill else None,
                            "width": (
                                round(gs.line_width * _scale_of(gs.ctm), 2)
                                if has_stroke
                                else None
                            ),
                            "dashes": gs.dashes,
                            "items": items,
                        }
                    )
            subpaths.clear()

        for operands, op in cs.operations:
            o = op.decode("latin1") if isinstance(op, bytes) else str(op)

            if o == "BT":
                in_text = True
                continue
            if o == "ET":
                in_text = False
                continue
            if in_text:
                continue

            if o == "q":
                stack.append(gs.copy())
            elif o == "Q":
                if stack:
                    gs = stack.pop()
            elif o == "cm" and len(operands) >= 6:
                local = tuple(_num(v) for v in operands[:6])
                gs.ctm = _mul(local, gs.ctm)  # type: ignore[arg-type]
            elif o == "w" and operands:
                gs.line_width = _num(operands[0])
            elif o == "d" and len(operands) >= 2:
                arr = operands[0]
                try:
                    vals = " ".join(str(round(_num(v), 2)) for v in arr)
                except TypeError:
                    vals = ""
                gs.dashes = (
                    f"[ {vals} ] {round(_num(operands[1]), 2)}" if vals else "[] 0"
                )
            elif o in ("RG", "SC", "SCN") and len(operands) >= 3:
                gs.stroke_color = tuple(  # type: ignore[assignment]
                    round(_num(v), 3) for v in operands[:3]
                )
            elif o == "G" and operands:
                v = round(_num(operands[0]), 3)
                gs.stroke_color = (v, v, v)
            elif o == "rg" and len(operands) >= 3:
                gs.fill_color = tuple(  # type: ignore[assignment]
                    round(_num(v), 3) for v in operands[:3]
                )
                gs.fill_pattern = None
            elif o == "g" and operands:
                v = round(_num(operands[0]), 3)
                gs.fill_color = (v, v, v)
                gs.fill_pattern = None
            elif o == "scn" and operands:
                # `/pN scn` after `/Pattern cs` selects a tiling pattern as
                # the fill colour - this is the construct pdfium cannot see.
                last = operands[-1]
                if isinstance(last, NameObject):
                    gs.fill_pattern = str(last)
                elif len(operands) >= 3:
                    gs.fill_color = tuple(  # type: ignore[assignment]
                        round(_num(v), 3) for v in operands[:3]
                    )
                    gs.fill_pattern = None
            elif o == "m" and len(operands) >= 2:
                pos = self._pt(gs.ctm, _num(operands[0]), _num(operands[1]))
                new_subpath(pos)
            elif o == "l" and len(operands) >= 2:
                p = self._pt(gs.ctm, _num(operands[0]), _num(operands[1]))
                if pos is not None and p != pos:
                    add_seg(("l", pos, p), p)
                pos = p
            elif o == "c" and len(operands) >= 6:
                p1 = self._pt(gs.ctm, _num(operands[0]), _num(operands[1]))
                p2 = self._pt(gs.ctm, _num(operands[2]), _num(operands[3]))
                p3 = self._pt(gs.ctm, _num(operands[4]), _num(operands[5]))
                if pos is not None:
                    add_seg(("c", pos, p1, p2, p3), p3)
                pos = p3
            elif o in ("v", "y") and len(operands) >= 4:
                a = self._pt(gs.ctm, _num(operands[0]), _num(operands[1]))
                b = self._pt(gs.ctm, _num(operands[2]), _num(operands[3]))
                if pos is not None:
                    add_seg(
                        ("c", pos, pos if o == "v" else a, a if o == "v" else b, b), b
                    )
                pos = b
            elif o == "re" and len(operands) >= 4:
                x, y = _num(operands[0]), _num(operands[1])
                w, h = _num(operands[2]), _num(operands[3])
                c0 = self._pt(gs.ctm, x, y)
                c1 = self._pt(gs.ctm, x + w, y + h)
                rect = _Rect(
                    min(c0.x, c1.x), min(c0.y, c1.y), max(c0.x, c1.x), max(c0.y, c1.y)
                )
                subpaths.append(
                    {"verts": [c0], "segs": [], "closed": True, "rect": rect}
                )
                pos = c0
            elif o == "h":
                if subpaths:
                    subpaths[-1]["closed"] = True
                    pos = subpaths[-1]["verts"][0]
            elif o in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"):
                if o in ("s", "b", "b*") and subpaths:
                    subpaths[-1]["closed"] = True
                # a pattern-filled region: the filled rect itself is just the
                # pattern's canvas and carries no data - descend into the
                # pattern's own content stream instead. This is the whole
                # reason this parser exists (pdfium cannot reach here).
                if o in ("f", "F", "f*") and gs.fill_pattern and resources is not None:
                    self._walk_pattern(gs.fill_pattern, resources, gs.ctm, depth)
                    subpaths.clear()
                else:
                    flush(o)
                pos = None
            elif o == "Do" and operands and resources is not None:
                self._walk_xobject(str(operands[0]), resources, gs.ctm, depth)

    # -- resource recursion -----------------------------------------------

    def _walk_xobject(
        self, name: str, resources: DictionaryObject, ctm: Matrix, depth: int
    ) -> None:
        try:
            xobjs = resources.get("/XObject")
            if xobjs is None:
                return
            xobjs = xobjs.get_object()
            xo = xobjs.get(name)
            if xo is None:
                return
            xo = xo.get_object()
            if str(xo.get("/Subtype")) != "/Form":
                return  # images carry no vector geometry
            mtx = xo.get("/Matrix")
            local = tuple(_num(v) for v in mtx) if mtx else _IDENTITY
            self.walk(
                xo.get_data(),
                xo.get("/Resources").get_object() if xo.get("/Resources") else None,
                _mul(local, ctm),  # type: ignore[arg-type]
                depth + 1,
            )
        except Exception:  # noqa: BLE001 - one bad xobject must not kill the walk
            return

    def _walk_pattern(
        self, name: str, resources: DictionaryObject, ctm: Matrix, depth: int
    ) -> None:
        try:
            pats = resources.get("/Pattern")
            if pats is None:
                return
            pats = pats.get_object()
            pat = pats.get(name)
            if pat is None:
                return
            pat = pat.get_object()
            if int(pat.get("/PatternType", 1)) != 1:
                return  # shading patterns carry no path geometry
            mtx = pat.get("/Matrix")
            local = tuple(_num(v) for v in mtx) if mtx else _IDENTITY
            self.walk(
                pat.get_data(),
                pat.get("/Resources").get_object() if pat.get("/Resources") else None,
                _mul(local, ctm),  # type: ignore[arg-type]
                depth + 1,
            )
        except Exception:  # noqa: BLE001
            return


class _RawStream:
    """Minimal shim so ContentStream can consume raw bytes."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def get_data(self) -> bytes:
        return self._data

    def get_object(self) -> "_RawStream":
        return self


def get_drawings(pdf_path: str, page_num: int) -> list[dict[str, Any]]:
    """PyMuPDF-`get_drawings()`-shaped output, parsed from raw content
    streams - reaches tiling-pattern content that PDFium's object API
    cannot."""
    reader = PdfReader(pdf_path)
    page = reader.pages[page_num]
    # Same visible-page transform as backend.pagespace: PyMuPDF space is
    # the mediabox-cropbox intersection with a top-left origin.
    media = page.mediabox
    crop = page.cropbox
    x_offset = max(float(media.left), float(crop.left))
    y_top = min(float(media.top), float(crop.top))

    walker = ContentWalker(y_top, x_offset)
    data = page.get_contents()
    if data is None:
        return []
    resources = page.get("/Resources")
    walker.walk(
        data.get_data(),
        resources.get_object() if resources is not None else None,
        _IDENTITY,
    )
    return walker.out


__all__ = ["get_drawings", "ContentWalker"]

_ = re  # keep the import meaningful if future ops need regex tokenising
