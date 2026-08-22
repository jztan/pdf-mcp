"""PyMuPDF-shaped get_drawings() reconstruction over pypdfium2.

Real port attempt (not just a feasibility probe) of the specific surface
chart_extractor.py actually reads from page.get_drawings(): the dict keys
`type`/`color`/`fill`/`width`/`dashes`/`items`, and items verbs
'l' (line), 're' (rect), 'c' (cubic bezier), 'qu' (quad) - confirmed by
grepping chart_extractor.py's path_pts()/rects_of()/draw_style().

Key finding this adapter had to solve that neither backend-evaluation.md
nor the earlier bounded feasibility check (pdfium_drawings.py) surfaced:
chart markers/curves commonly live inside nested Form XObjects with their
own local coordinate space (FPDF_PAGEOBJ_FORM, ~30/60 objects on
scatter_simple.pdf p1), not as flat page objects. Reaching them requires
recursive FPDFFormObj_CountObjects/GetObject traversal with composed
FS_MATRIX transforms - confirmed empirically: a form's matrix was a pure
translation (267.3, 200.1), its nested path's own matrix was identity, and
its local point coordinates were near-origin ((0.0, -2.12)), i.e. useless
without the parent's matrix applied.
"""

from __future__ import annotations

import ctypes
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from .geometry import Point as _Point
from .geometry import Quad as _Quad
from .geometry import Rect as _Rect

Matrix = tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _compose(parent: Matrix, child: Matrix) -> Matrix:
    """child-then-parent, PDF matrix convention: [a b 0; c d 0; e f 1]."""
    a1, b1, c1, d1, e1, f1 = child
    a2, b2, c2, d2, e2, f2 = parent
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


def _isotropic_scale(m: Matrix) -> float:
    """Determinant-based scale factor - standard technique for scaling a
    stroke width under a possibly-anisotropic transform. FPDFPageObj_Get-
    StrokeWidth returns the width in the object's own (pre-matrix) space;
    PyMuPDF's get_drawings() reports it already in page space. Confirmed
    empirically on a real-world PDF (2607.06338.pdf p7, inside a nested
    Form with matrix scale 1.0695): without this, stroke widths came back
    off by the form's scale factor, which changed a marker-size-keyed
    scatter series grouping (chart_extractor.draw_style buckets by
    rounded width) - not just point-value float noise, an actual
    structural point-group-count divergence (15 vs 17 groups)."""
    a, b, c, d, _, _ = m
    return float(abs(a * d - b * c) ** 0.5)


def _get_matrix(obj: Any) -> Matrix:
    m = pdfium_c.FS_MATRIX()
    pdfium_c.FPDFPageObj_GetMatrix(obj, ctypes.byref(m))
    return (m.a, m.b, m.c, m.d, m.e, m.f)


def _iter_path_objects(
    container: Any, count_fn: Any, get_fn: Any, matrix: Matrix
) -> Any:
    """Recurse through page/form objects, composing FS_MATRIX transforms,
    yielding (path_obj, composed_matrix) for every leaf path object."""
    n = count_fn(container)
    for i in range(n):
        obj = get_fn(container, i)
        otype = pdfium_c.FPDFPageObj_GetType(obj)
        obj_matrix = _compose(matrix, _get_matrix(obj))
        if otype == pdfium_c.FPDF_PAGEOBJ_FORM:
            yield from _iter_path_objects(
                obj,
                pdfium_c.FPDFFormObj_CountObjects,
                pdfium_c.FPDFFormObj_GetObject,
                obj_matrix,
            )
        elif otype == pdfium_c.FPDF_PAGEOBJ_PATH:
            yield obj, obj_matrix


def _raw_points(obj: Any, matrix: Matrix, transform: tuple[float, float]) -> list[Any]:
    """(seg_type, Point, is_close) in top-left page space, matrix + y-flip applied."""
    n = pdfium_c.FPDFPath_CountSegments(obj)
    out = []
    for i in range(n):
        seg = pdfium_c.FPDFPath_GetPathSegment(obj, i)
        lx, ly = ctypes.c_float(), ctypes.c_float()
        pdfium_c.FPDFPathSegment_GetPoint(seg, ctypes.byref(lx), ctypes.byref(ly))
        px, py = _apply(matrix, lx.value, ly.value)
        stype = pdfium_c.FPDFPathSegment_GetType(seg)
        close = bool(pdfium_c.FPDFPathSegment_GetClose(seg))
        x_off, y_top = transform
        out.append((stype, _Point(round(px - x_off, 2), round(y_top - py, 2)), close))
    return out


def _is_axis_aligned_rect(pts: list[_Point]) -> _Rect | None:
    if len(pts) != 4:
        return None
    xs = sorted({p.x for p in pts})
    ys = sorted({p.y for p in pts})
    if len(xs) != 2 or len(ys) != 2:
        return None
    return _Rect(xs[0], ys[0], xs[1], ys[1])


def _as_quad(pts: list[_Point]) -> _Quad | None:
    if len(pts) != 4:
        return None
    by_y = sorted(pts, key=lambda p: p.y)
    top, bottom = by_y[:2], by_y[2:]
    top = sorted(top, key=lambda p: p.x)
    bottom = sorted(bottom, key=lambda p: p.x)
    return _Quad(ul=top[0], ur=top[1], ll=bottom[0], lr=bottom[1])


def _subpath_to_items(points: list[tuple[int, _Point, bool]]) -> list[tuple[Any, ...]]:
    """One subpath's raw segments -> PyMuPDF-shaped item tuples."""
    if not points:
        return []
    verts = [p for _, p, _ in points]
    if len(set(verts)) <= 1:
        # single-point subpath (moveto immediately closed) draws nothing -
        # PyMuPDF's parser drops these; a naive item-builder would emit a
        # spurious zero-length 'l' segment for each one.
        return []
    closed = points[-1][2] or (len(verts) > 2 and verts[0] == verts[-1])
    unique_verts = verts[:-1] if (len(verts) > 1 and verts[0] == verts[-1]) else verts

    if closed:
        rect = _is_axis_aligned_rect(unique_verts)
        if rect is not None:
            return [("re", rect, 0)]
        quad = _as_quad(unique_verts) if len(unique_verts) == 4 else None
        if quad is not None:
            return [("qu", quad)]

    items: list[tuple[Any, ...]] = []
    i = 0
    while i < len(points) - 1:
        stype, pt, _ = points[i + 1]
        if stype == pdfium_c.FPDF_SEGMENT_BEZIERTO:
            # 3 consecutive BEZIERTO points = one cubic curve's control1,
            # control2, endpoint; p0 = current point before this run.
            p0 = points[i][1]
            if i + 3 >= len(points):
                break  # malformed / truncated curve run - drop rather than guess
            p1, p2, p3 = points[i + 1][1], points[i + 2][1], points[i + 3][1]
            items.append(("c", p0, p1, p2, p3))
            i += 3
        elif stype == pdfium_c.FPDF_SEGMENT_LINETO:
            start = points[i][1]
            if start != pt:
                # zero-length segments are real in the raw stream (e.g. the
                # explicit closing LINETO pdfium appends after a curve run
                # that already ends back at the start point) but draw
                # nothing - PyMuPDF's parser doesn't emit them either.
                items.append(("l", start, pt))
            i += 1
        else:
            i += 1
    return items


def get_drawings(page: pdfium.PdfPage) -> list[dict[str, Any]]:
    """PyMuPDF-shaped page.get_drawings() reconstruction, restricted to the
    dict keys chart_extractor.py actually reads."""
    from .pagespace import page_transform

    x_off, y_top = page_transform(page)
    out = []
    for obj, matrix in _iter_path_objects(
        page.raw, pdfium_c.FPDFPage_CountObjects, pdfium_c.FPDFPage_GetObject, _IDENTITY
    ):
        n_seg = pdfium_c.FPDFPath_CountSegments(obj)
        if n_seg == 0:
            continue
        raw = _raw_points(obj, matrix, (x_off, y_top))

        subpaths: list[list[tuple[int, _Point, bool]]] = []
        for entry in raw:
            if entry[0] == pdfium_c.FPDF_SEGMENT_MOVETO or not subpaths:
                subpaths.append([entry])
            else:
                subpaths[-1].append(entry)

        items: list[tuple[Any, ...]] = []
        for sp in subpaths:
            items.extend(_subpath_to_items(sp))
        if not items:
            continue

        fillmode = ctypes.c_int()
        stroke_flag = ctypes.c_int()
        pdfium_c.FPDFPath_GetDrawMode(
            obj, ctypes.byref(fillmode), ctypes.byref(stroke_flag)
        )
        has_fill = fillmode.value != pdfium_c.FPDF_FILLMODE_NONE
        has_stroke = bool(stroke_flag.value)

        color = None
        if has_stroke:
            r, g, b, a = (ctypes.c_uint() for _ in range(4))
            pdfium_c.FPDFPageObj_GetStrokeColor(
                obj, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a)
            )
            color = (
                round(r.value / 255, 3),
                round(g.value / 255, 3),
                round(b.value / 255, 3),
            )

        fill = None
        if has_fill:
            r, g, b, a = (ctypes.c_uint() for _ in range(4))
            pdfium_c.FPDFPageObj_GetFillColor(
                obj, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a)
            )
            fill = (
                round(r.value / 255, 3),
                round(g.value / 255, 3),
                round(b.value / 255, 3),
            )

        width = None
        if has_stroke:
            w = ctypes.c_float()
            pdfium_c.FPDFPageObj_GetStrokeWidth(obj, ctypes.byref(w))
            width = round(w.value * _isotropic_scale(matrix), 2)

        n_dashes = pdfium_c.FPDFPageObj_GetDashCount(obj)
        if n_dashes:
            scale = _isotropic_scale(matrix)
            arr = (ctypes.c_float * n_dashes)()
            pdfium_c.FPDFPageObj_GetDashArray(obj, arr, n_dashes)
            phase = ctypes.c_float()
            pdfium_c.FPDFPageObj_GetDashPhase(obj, ctypes.byref(phase))
            # dash array/phase are in the object's own space like stroke
            # width - same matrix-scale correction applies.
            vals = " ".join(str(round(v * scale, 2)) for v in arr)
            dashes = f"[ {vals} ] {round(phase.value * scale, 2)}"
        else:
            dashes = "[] 0"

        dtype = "fs" if (has_fill and has_stroke) else ("f" if has_fill else "s")
        out.append(
            {
                "type": dtype,
                "color": color,
                "fill": fill,
                "width": width,
                "dashes": dashes,
                "items": items,
            }
        )
    return out
