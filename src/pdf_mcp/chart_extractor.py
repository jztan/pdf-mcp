"""
Chart data extraction from born-digital vector PDFs (issue #23).

Pure-logic module: reads exact plotted geometry from PDF drawing commands and
calibrates it against tick-label text. Never guesses: ambiguity or failed
gates -> decline. Benchmarks: benchmark_data/chart_extraction/ (the regression
suite for this module; wrong-emit must stay 0).

extract_charts(doc, page_num, hints=None, max_points=24) -> {
  status: ok | needs_hint | declined,
  charts: [{chart_id, chart_type, x_axis, y_axis, curves|bars|points,
            diagnostics}],
  questions: [{id, kind, options}],  # when needs_hint (semantic ambiguity)
  reasons: [...],                   # when declined (a gate fired)
}
Hints are semantic enums only (never values): {"p0.s1.axis": "right",
"p0.type": "bar"}. Calibration + coordinates are always pure geometry, so a
wrong hint can mislabel an axis pairing at worst, never fabricate a number.

Tier-2 text self-answering (resolve_semantics): before asking the caller a
dual-axis question, matches a curve's stroke color against in-panel legend
entries and a rotated axis-title's tokens; a unique legend/title match
resolves the axis without a hint. Emitted curves carry "resolved_by"
("geometry" | "text" | "hint") and "label" (str | None).
"""

import hashlib
import json
import re
import collections
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf

CHART_EXTRACTION_VERSION = 2


def hints_hash(hints: dict[str, str] | None) -> str:
    """Stable short digest of a hints dict, order-independent. ``None`` and
    ``{}`` hash identically — both mean "no hints" for cache-key purposes."""
    return hashlib.sha1(json.dumps(hints or {}, sort_keys=True).encode()).hexdigest()[
        :16
    ]


# a drawing style key: (stroke_color, fill_color, line_width)
Style = tuple[Any, Any, float]


def _sig(v: Any, n: int = 4) -> float:
    """Round to ``n`` significant figures. Geometry-eyeballed chart values
    don't deserve more precision than this — 5g round-tripped through
    float() previously produced 15-digit fictional precision on log axes.
    Large-magnitude results may still print in integer/scientific notation
    in JSON; that is a JSON float-printing artifact, not extra precision."""
    return float(f"{float(v):.{n}g}")


def _style_dict(style_key: tuple[Any, ...]) -> dict[str, Any]:
    """Public, uniform series style shape: {"color": [r,g,b]|None, "width":
    float}. Accepts either the 3-tuple (stroke, fill, width) used
    internally by line/bar series, or the 2-tuple (color, size) used by
    scatter marker grouping."""
    if len(style_key) == 3:
        color, width = style_key[0], style_key[2]
    else:
        color, width = style_key
    return {"color": list(color) if color else None, "width": float(width)}


# ---------------- text/tick helpers (from v2, proven) ----------------


def get_words(page: Any) -> Any:
    return page.get_text("words")


def superscript_pow10(page: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw = page.get_text("rawdict")
    spans: list[dict[str, Any]] = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = "".join(c["c"] for c in span.get("chars", []))
                txt = txt.replace("−", "-")  # unicode minus in exponents
                spans.append(
                    {"t": txt.strip(), "size": span["size"], "bb": span["bbox"]}
                )
    for a in spans:
        if a["t"] != "10":
            continue
        for b in spans:
            if b is a or not re.fullmatch(r"-?\d{1,2}", b["t"]):
                continue
            if (
                b["size"] < 0.85 * a["size"]
                and 0 <= b["bb"][0] - a["bb"][2] < 3
                and b["bb"][1] < a["bb"][1] + 0.5
            ):
                x0, y0 = a["bb"][0], min(a["bb"][1], b["bb"][1])
                x1, y1 = b["bb"][2], max(a["bb"][3], b["bb"][3])
                out.append(
                    {
                        "v": 10.0 ** float(b["t"]),
                        "cx": (x0 + x1) / 2,
                        "cy": (y0 + y1) / 2,
                        "bb": (x0, y0, x1, y1),
                        "raw": f"10^{b['t']}",
                    }
                )
                break
    return out


def numeric_tokens(page: Any) -> list[dict[str, Any]]:
    sup = superscript_pow10(page)
    sup_boxes = [s["bb"] for s in sup]

    def in_sup(w: Any) -> bool:
        return any(
            w[0] >= b[0] - 1
            and w[2] <= b[2] + 1
            and w[1] >= b[1] - 1
            and w[3] <= b[3] + 1
            for b in sup_boxes
        )

    SUFFIX = {"k": 1e3, "K": 1e3, "M": 1e6, "B": 1e9, "G": 1e9, "T": 1e12}
    toks: list[dict[str, Any]] = []
    for w in get_words(page):
        if in_sup(w):
            continue
        t = w[4].strip().rstrip(".,;")
        t = t.replace("−", "-")  # unicode minus (matplotlib default)
        # locale-ambiguity gate: "5.000" is EN 5.0 but DE 5000 — parsing it
        # either way risks a silently mis-scaled axis (verified 1000x
        # wrong-emit). Unresolvable at token level -> drop; the axis then
        # declines for lack of ticks (safe). Leading-zero decimals ("0.395")
        # cannot be thousands-groups and stay. Comma-decimal with 1-2 digits
        # ("0,5") is unambiguous -> normalized to a decimal point.
        if re.fullmatch(r"-?[1-9]\d{0,2}(\.\d{3})+", t):
            continue
        if re.fullmatch(r"-?\d+,\d{1,2}", t):
            t = t.replace(",", ".")
        elif re.fullmatch(r"-?[1-9]\d{0,2}(,\d{3})+", t):
            continue  # EN thousands / DE ambiguity
        if re.fullmatch(r"-?\d+(\.\d+)?([eE]-?\d+)?", t):
            toks.append(
                {
                    "v": float(t),
                    "cx": (w[0] + w[2]) / 2,
                    "cy": (w[1] + w[3]) / 2,
                    "bb": w[:4],
                    "raw": t,
                }
            )
        else:
            # suffix-magnitude labels: 100M, 1.0B, 1T (ML/finance axes)
            m = re.fullmatch(r"(-?\d+(\.\d+)?)([kKMBGT])", t)
            if m:
                toks.append(
                    {
                        "v": float(m.group(1)) * SUFFIX[m.group(3)],
                        "cx": (w[0] + w[2]) / 2,
                        "cy": (w[1] + w[3]) / 2,
                        "bb": w[:4],
                        "raw": t,
                    }
                )
    return toks + sup


def cluster(
    toks: list[dict[str, Any]], key: Any, tol: float = 3.0
) -> list[list[dict[str, Any]]]:
    s = sorted(toks, key=key)
    groups: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = [s[0]] if s else []
    for t in s[1:]:
        if key(t) - key(cur[-1]) <= tol:
            cur.append(t)
        else:
            groups.append(cur)
            cur = [t]
    if cur:
        groups.append(cur)
    return groups


def monotonic_runs(
    g: list[dict[str, Any]], ck: str, min_len: int = 3
) -> list[list[dict[str, Any]]]:
    """Split a label cluster into maximal monotonic-value runs along the
    pixel coordinate. Small-multiple layouts put several subplots' ticks in
    one row/column cluster; each subplot's ticks form their own run."""
    g = sorted(g, key=lambda t: t[ck])
    runs: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = [g[0]] if g else []
    sign = 0
    for t in g[1:]:
        dv = t["v"] - cur[-1]["v"]
        s = (dv > 0) - (dv < 0)
        if s == 0:  # duplicate value: start a new run
            if len(cur) >= min_len:
                runs.append(cur)
            cur, sign = [t], 0
            continue
        if sign == 0 or s == sign:
            sign = s if sign == 0 else sign
            cur.append(t)
        else:
            if len(cur) >= min_len:
                runs.append(cur)
            cur, sign = [cur[-1], t], 0  # previous point may start next run
            sign = (t["v"] - cur[0]["v"] > 0) - (t["v"] - cur[0]["v"] < 0)
    if len(cur) >= min_len:
        runs.append(cur)
    return runs


def tick_series(g: list[dict[str, Any]], ck: str) -> dict[str, Any] | None:
    g = sorted(g, key=lambda t: t[ck])
    v = np.array([t["v"] for t in g])
    px = np.array([t[ck] for t in g])
    if len(g) < 3 or len(set(v.tolist())) < 3:
        return None
    dv, dpx = np.diff(v), np.diff(px)
    if not ((dv > 0).all() or (dv < 0).all()):
        return None
    if dpx.max() - dpx.min() > 0.35 * max(abs(dpx.mean()), 1):
        return None
    if abs(dv.max() - dv.min()) <= 0.25 * max(abs(dv.mean()), 1e-9):
        A = np.polyfit(px, v, 1)
        res = v - (A[0] * px + A[1])
        r2 = 1 - np.sum(res**2) / max(np.sum((v - v.mean()) ** 2), 1e-12)
        return {
            "scale": "linear",
            "a": A[0],
            "b": A[1],
            "px": px,
            "v": v,
            "r2": float(r2),
        }
    if (v > 0).all():
        lv = np.log10(v)
        dlv = np.diff(lv)
        if abs(dlv.max() - dlv.min()) <= 0.25 * max(abs(dlv.mean()), 1e-9):
            A = np.polyfit(px, lv, 1)
            res = lv - (A[0] * px + A[1])
            r2 = 1 - np.sum(res**2) / max(np.sum((lv - lv.mean()) ** 2), 1e-12)
            return {
                "scale": "log",
                "a": A[0],
                "b": A[1],
                "px": px,
                "v": v,
                "r2": float(r2),
            }
    return None


def apply_ax(ax: dict[str, Any], p: Any) -> Any:
    val = ax["a"] * np.asarray(p, float) + ax["b"]
    return 10 ** val if ax["scale"] == "log" else val


# ---------------- drawing helpers ----------------


def draw_style(d: dict[str, Any]) -> Style:
    stroke = tuple(round(x, 2) for x in d["color"]) if d.get("color") else None
    fill = tuple(round(x, 2) for x in d["fill"]) if d.get("fill") else None
    return (stroke, fill, round(d.get("width") or 0, 2))


def path_pts(d: dict[str, Any]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for it in d["items"]:
        if it[0] == "l":
            pts += [(it[1].x, it[1].y), (it[2].x, it[2].y)]
        elif it[0] == "c":
            # sample the cubic bezier — long smooth curves are drawn with few
            # segments, so endpoints alone starve the cloud (n<8 -> rejected)
            p0, p1, p2, p3 = it[1], it[2], it[3], it[4]
            for t in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
                mt = 1 - t
                x = (
                    mt**3 * p0.x
                    + 3 * mt**2 * t * p1.x
                    + 3 * mt * t**2 * p2.x
                    + t**3 * p3.x
                )
                y = (
                    mt**3 * p0.y
                    + 3 * mt**2 * t * p1.y
                    + 3 * mt * t**2 * p2.y
                    + t**3 * p3.y
                )
                pts.append((x, y))
        elif it[0] == "re":
            r = it[1]
            pts += [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
        elif it[0] == "qu":
            q = it[1]
            pts += [
                (q.ul.x, q.ul.y),
                (q.ur.x, q.ur.y),
                (q.ll.x, q.ll.y),
                (q.lr.x, q.lr.y),
            ]
    return pts


def d_bbox(d: dict[str, Any]) -> tuple[float, float, float, float] | None:
    pts = path_pts(d)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys)) if pts else None


def rects_of(d: dict[str, Any]) -> list[Any]:
    return [it[1] for it in d["items"] if it[0] == "re"]


# ---------------- panel detection ----------------


def axis_anchor_segments(
    page: Any,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """long axis-aligned segments + rect edges: candidate axis lines/frames"""
    horiz: list[tuple[float, float, float]] = []
    vert: list[tuple[float, float, float]] = []
    for d in page.get_drawings():
        for it in d["items"]:
            segs = []
            if it[0] == "l":
                segs = [(it[1], it[2])]
            elif it[0] == "re":
                r = it[1]
                horiz += [(r.x0, r.x1, r.y0), (r.x0, r.x1, r.y1)]
                vert += [(r.y0, r.y1, r.x0), (r.y0, r.y1, r.x1)]
                continue
            for a, b in segs:
                if abs(a.y - b.y) < 1.0 and abs(a.x - b.x) > 20:
                    horiz.append((min(a.x, b.x), max(a.x, b.x), a.y))
                elif abs(a.x - b.x) < 1.0 and abs(a.y - b.y) > 20:
                    vert.append((min(a.y, b.y), max(a.y, b.y), a.x))
    return horiz, vert


def _looks_like_colorbar(page: Any, x1: float, ya: dict[str, Any]) -> bool:
    """Defense in depth against the arXiv 2001.08361 p24 Fig18 wrong-emit: a
    matplotlib colorbar is a narrow vertical strip — a raster image OR a
    dense stack of thin filled rects — sitting immediately left of its own
    tick-label column. ``x1`` is the x-axis span's right end (the panel's
    right edge); ``ya`` is a y-axis candidate being evaluated as a RIGHT-side
    axis. Returns True when the horizontal band between ``x1`` and the
    candidate's tick-label column (``ya["x_at"]``) is occupied by
    colorbar-shaped content: narrow (< 35pt wide) and tall enough
    (>= 0.4x the candidate's own tick-label pixel span) to plausibly be the
    strip those labels are ticking."""
    x_at = ya["x_at"]
    band_x0, band_x1 = (x1, x_at) if x1 <= x_at else (x_at, x1)
    py_span = float(ya["px"].max() - ya["px"].min())
    min_h = 0.4 * py_span
    for info in page.get_image_info():
        bbox = info.get("bbox")
        if not bbox:
            continue
        bx0, by0, bx1, by1 = bbox
        if (
            bx0 >= band_x0 - 2
            and bx1 <= band_x1 + 2
            and (bx1 - bx0) < 35
            and (by1 - by0) >= min_h
        ):
            return True
    fills: list[Any] = []
    for d in page.get_drawings():
        if d.get("fill") is None:
            continue
        for r in rects_of(d):
            if band_x0 - 2 <= r.x0 and r.x1 <= band_x1 + 2:
                fills.append(r)
    if len(fills) >= 8:
        w = max(r.x1 for r in fills) - min(r.x0 for r in fills)
        h = max(r.y1 for r in fills) - min(r.y0 for r in fills)
        if w < 35 and h >= min_h:
            return True
    return False


def find_panels(page: Any) -> list[dict[str, Any]]:
    toks = numeric_tokens(page)
    horiz, vert = axis_anchor_segments(page)
    rows = [g for g in cluster(toks, lambda t: t["cy"]) if len(g) >= 3]
    # y-axis labels are edge-aligned (right edge for a left axis, left edge
    # for a right axis) — centers shift with digit count, edges don't.
    cols: list[list[dict[str, Any]]] = []
    seen_sets: list[frozenset[int]] = []
    for key in (lambda t: t["bb"][2], lambda t: t["bb"][0], lambda t: t["cx"]):
        for g in cluster(toks, key):
            if len(g) < 3:
                continue
            ids = frozenset(id(t) for t in g)
            # exact-duplicate dedup only: a clean SUBSET cluster (e.g. the
            # same labels without a stray caption token) must survive even
            # when a polluted superset was seen first
            if ids in seen_sets:
                continue
            seen_sets.append(ids)
            cols.append(g)
    x_axes: list[dict[str, Any]] = []
    y_axes: list[dict[str, Any]] = []
    for g0 in rows:
        for g in monotonic_runs(g0, "cx"):
            if max(t["cx"] for t in g) - min(t["cx"] for t in g) < 45:
                continue
            s = tick_series(g, "cx")
            if not s:
                continue
            # anchored-axis check: a real x-label row sits just below a long
            # horizontal axis line spanning most of its range. Kills "fake
            # rows" assembled from side-by-side subplots' y-labels (their
            # gridlines are per-panel, too short to span the fake run).
            y_at = float(np.mean([t["cy"] for t in g]))
            x0, x1 = s["px"].min(), s["px"].max()
            anchors = [
                (hx0, hx1, hy)
                for hx0, hx1, hy in horiz
                if hx0 <= x0 + 10 and hx1 >= x1 - 10 and y_at - 25 <= hy <= y_at - 1
            ]
            if not anchors:
                continue
            s["y_at"] = y_at
            # segment nearest the labels = the axis line / frame bottom edge
            # (NOT the longest — that can be the page-background rect edge)
            s["anchor"] = min(anchors, key=lambda a: y_at - a[2])
            x_axes.append(s)
    for g0 in cols:
        for g in monotonic_runs(g0, "cy"):
            if max(t["cy"] for t in g) - min(t["cy"] for t in g) < 45:
                continue
            s = tick_series(g, "cy")
            if not s:
                continue
            x_at = float(np.mean([t["cx"] for t in g]))
            y0, y1 = s["px"].min(), s["px"].max()
            anchors = [
                (vy0, vy1, vx)
                for vy0, vy1, vx in vert
                if vy0 <= y0 + 10 and vy1 >= y1 - 10 and abs(vx - x_at) <= 35
            ]
            if not anchors:
                continue
            s["x_at"] = x_at
            s["anchor"] = min(anchors, key=lambda a: abs(a[2] - x_at))
            y_axes.append(s)
    TOL = 30.0
    panels: list[dict[str, Any]] = []
    for xa in x_axes:
        x0, x1 = xa["px"].min(), xa["px"].max()

        xspan = x1 - x0

        def corner_ok(ya: dict[str, Any]) -> bool:
            # same-plot constraint: x and y axes must meet at a shared corner.
            # (1) x-axis runs along the BOTTOM of the y-axis's vertical span.
            # (2) y-axis sits within a horizontal band around the x-axis span.
            # Rejects pairing axes from two different figures on one page
            # (they fail one or both), while tolerating the normal gap between
            # y-tick labels and the first x-tick.
            y0, y1 = ya["px"].min(), ya["px"].max()
            # x-axis label row sits at/below the plot bottom; allow a generous
            # downward margin for the gap between lowest y-label and x-labels.
            vert = (y0 - TOL) <= xa["y_at"] <= (y1 + 60)
            band = max(0.4 * xspan, 80)
            horiz = (x0 - band) <= ya["x_at"] <= (x1 + band)
            return bool(vert and horiz)

        # anchor-corner consistency: the y-axis spine must meet the x-axis
        # anchor line at a shared corner. A neighboring subplot's spine does
        # not line up with THIS panel's frame edge.
        hx0a, hx1a, _ = xa["anchor"]

        def corner_meets(ya: dict[str, Any], end_x: float) -> bool:
            return bool(abs(ya["anchor"][2] - end_x) <= 15)

        lefts = [
            ya
            for ya in y_axes
            if ya["x_at"] < x0 + 20
            and ya["px"].max() <= xa["y_at"] + 25
            and corner_ok(ya)
            and corner_meets(ya, hx0a)
        ]
        # a true right axis hugs the panel's right edge; anything further out
        # is a NEIGHBORING subplot's left axis (small-multiple layouts)
        rights = [
            ya
            for ya in y_axes
            if x1 - 20 < ya["x_at"] <= x1 + 45
            and ya["px"].max() <= xa["y_at"] + 25
            and corner_ok(ya)
            and corner_meets(ya, hx1a)
            and not _looks_like_colorbar(page, x1, ya)
        ]
        if not lefts and not rights:
            continue
        # more ticks = better axis (half-columns from label-cluster splits
        # lose to the full column), then nearest to the plot edge
        lefts.sort(key=lambda ya: (-len(ya["px"]), x0 - ya["x_at"]))
        rights.sort(key=lambda ya: (-len(ya["px"]), ya["x_at"] - x1))
        ya = lefts[0] if lefts else rights[0]
        panels.append(
            {
                "xa": xa,
                "ya": ya,
                "ya_left": lefts[0] if lefts else None,
                "ya_right": rights[0] if rights else None,
                "rx0": x0 - 10,
                "rx1": x1 + 15,
                "ry0": min(ya["px"].min(), rights[0]["px"].min() if rights else 1e9)
                - 15,
                "ry1": xa["y_at"] + 2,
            }
        )
    return panels


# ---------------- structural filters ----------------


def frame_like(d: dict[str, Any], panel: dict[str, Any]) -> bool:
    """large axis-aligned rect ~ spanning the plot region, or full-span line"""
    bb = d_bbox(d)
    if bb is None:
        return False
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    pw, ph = panel["rx1"] - panel["rx0"], panel["ry1"] - panel["ry0"]
    if w > 0.85 * pw and h > 0.85 * ph:
        return True  # plot frame box
    pts = path_pts(d)
    if len(pts) >= 2:
        aligned = all(
            abs(a[0] - b[0]) < 1.2 or abs(a[1] - b[1]) < 1.2
            for a, b in zip(pts[:-1], pts[1:])
        )
        if aligned and (w > 0.9 * pw or h > 0.9 * ph) and min(w, h) < 2.5:
            return True  # gridline / axis line
        # per-SEGMENT alignment (pen jumps between strokes are diagonal, so
        # test the drawn line items, not consecutive sampled points): a path
        # whose every stroke is axis-aligned is decoration when it is either
        # (a) a grid lattice spanning the plot, or (b) a thin strip (tick row/
        # column, partial gridline). Trade-off: (b) also drops a perfectly
        # flat data line (rare; documented).
        segs = [(it[1], it[2]) for it in d["items"] if it[0] == "l"]
        if len(segs) >= 3 and all(
            abs(a.x - b.x) < 1.2 or abs(a.y - b.y) < 1.2 for a, b in segs
        ):
            # connected chain of aligned strokes = a STEP FUNCTION (data),
            # not decoration: grids/tick strips are disjoint strokes.
            joined = sum(
                1
                for (a1, b1), (a2, b2) in zip(segs[:-1], segs[1:])
                if abs(b1.x - a2.x) < 1.0 and abs(b1.y - a2.y) < 1.0
            )
            if joined >= 0.8 * (len(segs) - 1):
                return False
            if w > 0.5 * pw and h > 0.5 * ph:
                return True  # grid lattice
            if min(w, h) < 3:
                return True  # tick strip / partial gridline
    return False


def legend_masks(
    page: Any, panel: dict[str, Any]
) -> list[tuple[float, float, float, float]]:
    masks: list[tuple[float, float, float, float]] = []
    for w in get_words(page):
        if re.fullmatch(r"-?\d+(\.\d+)?", w[4].strip()):
            continue
        if (
            panel["rx0"] <= w[0]
            and w[2] <= panel["rx1"]
            and panel["ry0"] <= w[1]
            and w[3] <= panel["ry1"]
        ):
            masks.append((w[0] - 45, w[1] - 3, w[2] + 3, w[3] + 3))
    return masks


def masked(x: float, y: float, masks: list[tuple[float, float, float, float]]) -> bool:
    return any(m[0] <= x <= m[2] and m[1] <= y <= m[3] for m in masks)


def _looks_like_axis_title(text: str) -> bool:
    """A real axis title is a short label, not a sentence or caption."""
    t = text.strip()
    if not t or len(t) > 45:
        return False
    if len(t.split()) > 6:
        return False
    if re.match(r"(?i)^(figure|fig|table|eq|equation)\b", t):
        return False
    if re.search(r",\s", t):  # sentence-like clause separator
        return False
    if re.fullmatch(r"-?\d+(\.\d+)?", t):  # a stray number
        return False
    return True


# ---------------- tier-2 text self-answering (legend + axis titles) --------


def _legend_entries(page: Any, panel: dict[str, Any]) -> list[tuple[Style, str]]:
    """Legend entries: a short stroked sample next to a text label inside
    the panel. Returns [(style_key, label_text), ...]."""
    entries: list[tuple[Style, str]] = []
    words = [
        w
        for w in get_words(page)
        if panel["rx0"] <= w[0]
        and w[2] <= panel["rx1"]
        and panel["ry0"] <= w[1]
        and w[3] <= panel["ry1"]
        and not re.fullmatch(r"-?[\d.,]+", w[4].strip())
    ]
    if not words:
        return entries
    # group words into lines (same baseline)
    words.sort(key=lambda w: (round(w[3]), w[0]))
    lines: list[list[Any]] = []
    cur: list[Any] = [words[0]]
    for w in words[1:]:
        if abs(w[3] - cur[-1][3]) < 2 and w[0] - cur[-1][2] < 12:
            cur.append(w)
        else:
            lines.append(cur)
            cur = [w]
    lines.append(cur)
    for ln in lines:
        x0, y0, y1 = ln[0][0], ln[0][1], ln[0][3]
        label = " ".join(w[4] for w in ln).strip()
        # sample stroke: a drawing to the left of the label, vertically
        # centered on the line, short (< 45pt wide)
        for d in page.get_drawings():
            bb = d_bbox(d)
            if bb is None or d.get("color") is None:
                continue
            if (
                x0 - 48 <= bb[0]
                and bb[2] <= x0 - 2
                and bb[1] >= y0 - 4
                and bb[3] <= y1 + 4
                and bb[2] - bb[0] >= 8
            ):
                entries.append((draw_style(d), label))
                break
    return entries


def _axis_titles(page: Any, panel: dict[str, Any]) -> dict[str, str | None]:
    """Axis titles: rotated text near the left/right panel edges (y-axis
    titles) via get_text('dict') line direction. Returns
    {"left": str|None, "right": str|None}."""
    out: dict[str, str | None] = {"left": None, "right": None}
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            if abs(line.get("dir", (1, 0))[0]) > 0.3:
                continue  # not vertical text
            bb = line["bbox"]
            if not (panel["ry0"] - 10 <= bb[1] and bb[3] <= panel["ry1"] + 10):
                continue
            text = " ".join(s["text"] for s in line["spans"]).strip()
            if not text or not _looks_like_axis_title(text):
                continue
            if bb[2] <= panel["rx0"] + 10:
                out["left"] = text
            elif bb[0] >= panel["rx1"] - 10:
                out["right"] = text
    return out


def _x_axis_title(page: Any, panel: dict[str, Any]) -> str | None:
    """x-axis title: horizontal text centered under the tick-label row,
    within a plausible band below the panel. Returns the nearest such line,
    or None (display string only — never parsed as data)."""
    d = page.get_text("dict")
    cx_target = (panel["rx0"] + panel["rx1"]) / 2
    pw = max(panel["rx1"] - panel["rx0"], 1e-6)
    best: tuple[float, str] | None = None
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            if abs(line.get("dir", (1, 0))[1]) > 0.3:
                continue  # not horizontal text
            bb = line["bbox"]
            if not (panel["ry1"] + 2 <= bb[1] <= panel["ry1"] + 35):
                continue
            text = " ".join(s["text"] for s in line["spans"]).strip()
            if not text or not _looks_like_axis_title(text):
                continue
            line_cx = (bb[0] + bb[2]) / 2
            if abs(line_cx - cx_target) > 0.3 * pw:
                continue
            dist = abs(bb[1] - panel["ry1"])
            if best is None or dist < best[0]:
                best = (dist, text)
    return best[1] if best else None


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def resolve_semantics(
    page: Any,
    panel: dict[str, Any],
    curves: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[int, str]]:
    """Tier-2: answer open questions from the PDF's own text. Returns
    (answers: {question_id: enum}, labels: {series_index: label})."""
    entries = _legend_entries(page, panel)
    # style collision -> that style identifies nothing; drop colliding entries
    style_counts = collections.Counter(e[0][0] for e in entries)  # by stroke color
    entries = [e for e in entries if style_counts[e[0][0]] == 1]
    titles = _axis_titles(page, panel)
    answers: dict[str, str] = {}
    labels: dict[int, str] = {}
    for q in questions:
        if q["kind"] != "y_axis_for_curve":
            continue
        s_idx = int(q["id"].split(".s")[1].split(".")[0])
        curve = curves[s_idx]
        col = curve["_style_key"][0]
        # entries key on the full draw_style tuple (stroke, fill, width);
        # curves only carry the stroke color here — match on stroke alone.
        label = next((lab for st, lab in entries if st[0] == col), None)
        if label is None:
            continue
        labels[s_idx] = label
        lt = _tokens(label)
        left_hit = titles["left"] and lt & _tokens(titles["left"])
        right_hit = titles["right"] and lt & _tokens(titles["right"])
        if left_hit and not right_hit:
            answers[q["id"]] = "left"
        elif right_hit and not left_hit:
            answers[q["id"]] = "right"
        # both/neither -> stays a question (unique match only)
    return answers, labels


# ---------------- per-type extraction ----------------


def in_panel(
    bb: tuple[float, float, float, float] | None,
    panel: dict[str, Any],
    frac: float = 0.9,
) -> bool:
    if bb is None:
        return False
    w = max(bb[2] - bb[0], 1e-6)
    h = max(bb[3] - bb[1], 1e-6)
    ix = max(0, min(bb[2], panel["rx1"]) - max(bb[0], panel["rx0"]))
    iy = max(0, min(bb[3], panel["ry1"]) - max(bb[1], panel["ry0"]))
    return bool((ix * iy) / (w * h) >= frac)


def collect(
    draws: list[dict[str, Any]],
    panel: dict[str, Any],
    masks: list[tuple[float, float, float, float]],
) -> tuple[
    list[dict[str, Any]],
    list[tuple[Any, Style]],
    dict[tuple[Any, int], list[tuple[float, float]]],
    dict[Style, list[tuple[float, float]]],
]:
    """classify in-panel drawings into frames, bars(candidate rects),
    markers (congruent small paths), polyline clouds by style"""
    frames: list[dict[str, Any]] = []
    bar_rects: list[tuple[Any, Style]] = []
    small_paths: dict[tuple[Any, int], list[tuple[float, float]]] = (
        collections.defaultdict(list)
    )
    clouds: dict[Style, list[tuple[float, float]]] = collections.defaultdict(list)
    pw = panel["rx1"] - panel["rx0"]
    ph = panel["ry1"] - panel["ry0"]
    for d in draws:
        bb = d_bbox(d)
        if bb is None:
            continue
        intersects = not (
            bb[2] < panel["rx0"]
            or bb[0] > panel["rx1"]
            or bb[3] < panel["ry0"]
            or bb[1] > panel["ry1"]
        )
        if intersects and frame_like(d, panel):
            frames.append(d)
            continue
        if not in_panel(bb, panel):
            continue
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        # tick-mark stubs: tiny paths whose segments are all axis-aligned
        # (fail the marker aspect check, then pollute clouds as baseline
        # noise). Pure decoration — skip outright.
        segs = [(it[1], it[2]) for it in d["items"] if it[0] == "l"]
        if (
            segs
            and max(w, h) < 8
            and all(abs(a.x - b.x) < 1.0 or abs(a.y - b.y) < 1.0 for a, b in segs)
        ):
            continue
        # marker glyph: small, square-ish path (filled or stroked). Check
        # BEFORE bars so small filled marker-rects aren't misread as bars.
        mcap = 0.09 * min(pw, ph)
        if (
            max(w, h) <= mcap
            and max(w, h) <= 12
            and 0.35 <= (w + 1e-6) / (h + 1e-6) <= 2.8
        ):
            if masked(cx, cy, masks):
                continue
            col = draw_style(d)
            # key by color (ignore fill/stroke split) + rounded size bucket
            ckey = (col[1] or col[0], round(max(w, h)))
            small_paths[ckey].append((cx, cy))
            continue
        rs = rects_of(d)
        if rs and d.get("fill") is not None:
            for r in rs:
                if r.width < 0.5 * pw and r.height <= ph:
                    bar_rects.append((r, draw_style(d)))
            continue
        for x, y in path_pts(d):
            if not masked(x, y, masks):
                clouds[draw_style(d)].append((x, y))
    return frames, bar_rects, small_paths, clouds


def classify(
    bar_rects: list[tuple[Any, Style]],
    small_paths: dict[tuple[Any, int], list[tuple[float, float]]],
    clouds: dict[Style, list[tuple[float, float]]],
    panel: dict[str, Any],
) -> str:
    pw = panel["rx1"] - panel["rx0"]
    # bars: >=3 filled rects sharing a baseline (same bottom y)
    if len(bar_rects) >= 3:
        bottoms = collections.Counter(round(r.y1, 1) for r, s in bar_rects)
        base, n = bottoms.most_common(1)[0]
        if n >= 3:
            return "bar"
    markers = {k: v for k, v in small_paths.items() if len(v) >= 5}
    lines = {}
    for k, pts in clouds.items():
        xs = [p[0] for p in pts]
        if len(pts) >= 8 and max(xs) - min(xs) >= 0.25 * pw:
            lines[k] = pts
    if lines:
        return "line"
    if markers:
        return "scatter"
    return "unknown"


def _select_sample_indices(dy: np.ndarray, max_points: int) -> np.ndarray:
    """Choose <= max_points indices into ``dy`` for downsampled emission.

    Always keeps the series endpoints plus the global argmin/argmax of
    ``dy`` (a table must not silently lose the peak/trough), fills the
    remaining budget with local extrema ranked by prominence
    (|y - mean(neighbors)|), then pads with a uniform spread. Returns
    sorted unique indices.
    """
    forced = {0, len(dy) - 1, int(np.argmax(dy)), int(np.argmin(dy))}
    # local extrema (sign change of dy differences), ranked by
    # prominence = |y - mean of neighbors|
    d1 = np.diff(dy)
    ext = np.where(np.sign(d1[:-1]) * np.sign(d1[1:]) < 0)[0] + 1
    prom = np.abs(dy[ext] - (dy[ext - 1] + dy[ext + 1]) / 2)
    ranked = ext[np.argsort(prom)[::-1]]
    remaining_budget = max(0, max_points - len(forced))
    keep_ext = ranked[:remaining_budget]
    fill = max(0, max_points - len(forced) - len(keep_ext))
    uniform = np.linspace(0, len(dy) - 1, fill).astype(int)
    sel: np.ndarray = np.unique(np.concatenate([list(forced), keep_ext, uniform]))
    return sel


def extract_line(
    clouds: dict[Style, list[tuple[float, float]]],
    panel: dict[str, Any],
    xa: dict[str, Any],
    ya: dict[str, Any],
    max_points: int,
) -> list[dict[str, Any]]:
    pw = panel["rx1"] - panel["rx0"]
    ph = panel["ry1"] - panel["ry0"]
    curves: list[dict[str, Any]] = []
    for k, pts in clouds.items():
        xs = np.array([p[0] for p in pts])
        ys = np.array([p[1] for p in pts])
        if len(pts) < 8 or np.ptp(xs) < 0.25 * pw:
            continue
        order = np.argsort(xs)
        xs, ys = xs[order], ys[order]
        bins = np.linspace(xs.min(), xs.max(), 24)
        idx = np.digitize(xs, bins)
        spreads = [np.ptp(ys[idx == j]) for j in range(1, 25) if (idx == j).sum() >= 2]
        if spreads and np.median(spreads) > 0.12 * ph:
            curves.append({"_style_key": k, "multivalued": True})
            continue
        dx = apply_ax(xa, xs)
        dy = apply_ax(ya, ys)
        order = np.argsort(dx)
        dx, dy = dx[order], dy[order]
        # path_pts duplicates each interior vertex (line-segment end/start
        # overlap), which zeroes out d1 at every vertex and defeats the
        # sign-change extrema test below — collapse exact consecutive
        # duplicates first so extrema detection sees the real polyline.
        keep = np.concatenate([[True], (np.diff(dx) != 0) | (np.diff(dy) != 0)])
        dx, dy = dx[keep], dy[keep]
        n_extrema_dropped = 0
        if len(dx) <= max_points:
            sel = np.arange(len(dx))
            downsampled = False
        else:
            sel = _select_sample_indices(dy, max_points)
            # local extrema (sign change of dy differences) not present in
            # the final selection were dropped for lack of budget
            d1 = np.diff(dy)
            ext = np.where(np.sign(d1[:-1]) * np.sign(d1[1:]) < 0)[0] + 1
            n_extrema_dropped = int(np.setdiff1d(ext, sel).size)
            downsampled = True
        curves.append(
            {
                "_style_key": k,
                "multivalued": False,
                "downsampled": downsampled,
                "n_extrema_dropped": int(n_extrema_dropped),
                "points": [[_sig(dx[i]), _sig(dy[i])] for i in sel],
            }
        )
    return curves


def extract_bar(
    bar_rects: list[tuple[Any, Style]], xa: dict[str, Any], ya: dict[str, Any]
) -> list[dict[str, Any]]:
    by_style: dict[Style, list[Any]] = collections.defaultdict(list)
    for r, s in bar_rects:
        by_style[s].append(r)
    series: list[dict[str, Any]] = []
    yv = ya["v"]
    y_lo, y_rng = float(min(yv)), float(max(yv)) - float(min(yv))
    for s, rs in by_style.items():
        if len(rs) < 3:
            continue
        # bars must stand on the axis baseline: the series' common bottom
        # edge has to map to ~the y-axis minimum. A marginal-distribution
        # histogram (drawn in the plot margins with its own local zero) maps
        # to a random mid-axis value and is rejected here.
        base_py = collections.Counter(round(r.y1, 1) for r in rs).most_common(1)[0][0]
        base_val = float(apply_ax(ya, base_py))
        # one-sided: the true baseline may sit below the lowest LABELED tick
        # (charts often leave 0 unlabeled), but never meaningfully above it
        if base_val - y_lo > 0.1 * max(y_rng, 1e-9):
            continue
        pts: list[list[float]] = []
        for r in rs:
            cx = (r.x0 + r.x1) / 2
            pts.append(
                [_sig(apply_ax(xa, cx)), _sig(apply_ax(ya, r.y0))]
            )  # top edge = value
        pts.sort()
        series.append({"_style_key": s, "bars": pts})
    return series


def extract_scatter(
    small_paths: dict[tuple[Any, int], list[tuple[float, float]]],
    xa: dict[str, Any],
    ya: dict[str, Any],
) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    for (ckey, size), centers in small_paths.items():
        # merge fill+stroke duplicates drawn at the same location
        uniq: list[tuple[float, float]] = []
        for cx, cy in sorted(centers):
            if not any(abs(cx - ux) < 1.5 and abs(cy - uy) < 1.5 for ux, uy in uniq):
                uniq.append((cx, cy))
        if len(uniq) < 5:
            continue
        pts = [[_sig(apply_ax(xa, cx)), _sig(apply_ax(ya, cy))] for cx, cy in uniq]
        pts.sort()
        series.append({"_style_key": (ckey, size), "marker_size": size, "points": pts})
    return series


# ---------------- main entry ----------------


def _range(ax: dict[str, Any]) -> tuple[float, float]:
    v = ax["v"]
    return float(min(v)), float(max(v))


def in_range_series(
    pts: list[Any],
    xr: tuple[float, float],
    yr: tuple[float, float],
    frac: float = 0.15,
    need: float = 0.7,
) -> bool:
    """keep only series where >=need fraction of points fall within the
    tick range (+/- margin). Marginal-distribution bars / decorations that
    extend into the plot margins map outside the axis range and are dropped."""
    if not pts:
        return False
    xm = frac * max(xr[1] - xr[0], 1e-9)
    ym = frac * max(yr[1] - yr[0], 1e-9)
    ok = sum(
        1
        for x, y in pts
        if xr[0] - xm <= x <= xr[1] + xm and yr[0] - ym <= y <= yr[1] + ym
    )
    return ok / len(pts) >= need


def extract_charts(
    doc: Any,
    page_num: int,
    hints: dict[str, str] | None = None,
    max_points: int = 24,
) -> dict[str, Any]:
    """Extract chart series from doc[page_num] (0-indexed).

    Returns {"status": "ok"|"needs_hint"|"declined", "charts": [...],
    "questions": [...], "reasons": [...]}. Never raises on chart-shaped
    problems; gates decline instead.
    """
    hints = hints or {}
    # up-front value validation: closed enums per hint-id suffix. Ids
    # themselves are validated later (after extraction) by checking which
    # supplied hint keys were actually consumed by a real panel/series.
    _AXIS_VALUES = {"left", "right"}
    _TYPE_VALUES = {"line", "bar", "scatter", "not_a_chart"}
    for hk, hv in hints.items():
        suffix = hk.rsplit(".", 1)[-1]
        if suffix == "axis" and hv not in _AXIS_VALUES:
            return {"error": f"invalid hint value {hv!r} for {hk}"}
        if suffix == "type" and hv not in _TYPE_VALUES:
            return {"error": f"invalid hint value {hv!r} for {hk}"}
    used_hint_keys: set[str] = set()
    max_points = max(max_points, 4)
    page = doc[page_num]
    res: dict[str, Any] = {
        "page": page_num + 1,
        "status": "ok",
        "charts": [],
        "questions": [],
        "reasons": [],
    }
    panels = find_panels(page)
    if not panels:
        if hints:
            return {"error": f"unknown hint id: {sorted(hints)[0]}"}
        res["status"] = "declined"
        res["reasons"].append("no chart signature (no valid tick-series axes)")
        return res
    draws = page.get_drawings()
    for pi, panel in enumerate(panels):
        xa, ya = panel["xa"], panel["ya"]
        masks = legend_masks(page, panel)
        frames, bar_rects, small_paths, clouds = collect(draws, panel, masks)
        # refine region: if a plot-frame box was found, adopt it (tick-label
        # spans undershoot the true plot area) and re-collect
        pw0 = panel["rx1"] - panel["rx0"]
        ph0 = panel["ry1"] - panel["ry0"]

        def mutual_overlap(bb: tuple[float, float, float, float]) -> bool:
            # frame must mostly overlap THIS panel (and vice versa) — a
            # neighboring subplot's frame merely touches the region edge
            ix = max(0, min(bb[2], panel["rx1"]) - max(bb[0], panel["rx0"]))
            iy = max(0, min(bb[3], panel["ry1"]) - max(bb[1], panel["ry0"]))
            inter = ix * iy
            fa = (bb[2] - bb[0]) * (bb[3] - bb[1])
            pa = pw0 * ph0
            return bool(inter >= 0.5 * fa and inter >= 0.5 * pa)

        big = [
            v3bb
            for v3bb in (d_bbox(f) for f in frames)
            if v3bb
            and 0.7 * pw0 < (v3bb[2] - v3bb[0]) < 1.4 * pw0
            and 0.5 * ph0 < (v3bb[3] - v3bb[1]) < 1.4 * ph0
            and mutual_overlap(v3bb)
        ]
        if big:
            fb = max(big, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            panel = dict(
                panel, rx0=fb[0] - 2, rx1=fb[2] + 2, ry0=fb[1] - 2, ry1=fb[3] + 2
            )
            masks = legend_masks(page, panel)
            frames, bar_rects, small_paths, clouds = collect(draws, panel, masks)
        ctype = classify(bar_rects, small_paths, clouds, panel)
        # hint override
        tkey = f"p{pi}.type"
        if tkey in hints:
            ctype = hints[tkey]
            used_hint_keys.add(tkey)
        y_side = "left" if panel["ya_left"] else "right"
        titles = _axis_titles(page, panel)
        chart: dict[str, Any] = {
            "chart_id": f"p{pi}",
            "panel": pi,
            "chart_type": ctype,
            "region_bbox": [
                float(panel["rx0"]),
                float(panel["ry0"]),
                float(panel["rx1"]),
                float(panel["ry1"]),
            ],
            "x_axis": {
                "scale": xa["scale"],
                "r2": round(xa["r2"], 5),
                "title": _x_axis_title(page, panel),
                "range": [float(xa["v"].min()), float(xa["v"].max())],
            },
            "y_axis": {
                "scale": ya["scale"],
                "r2": round(ya["r2"], 5),
                "side": y_side,
                "title": titles.get(y_side),
                "range": [float(ya["v"].min()), float(ya["v"].max())],
            },
            "diagnostics": {
                "n_frames": len(frames),
                "n_bar_rects": len(bar_rects),
                "n_marker_groups": len(
                    [1 for v in small_paths.values() if len(v) >= 5]
                ),
                "n_line_clouds": len(clouds),
                "dual_axis": bool(panel["ya_left"] and panel["ya_right"]),
                "notes": [],
            },
        }
        if panel["ya_left"] and panel["ya_right"]:
            ya_r = panel["ya_right"]
            chart["y_axis_right"] = {
                "scale": ya_r["scale"],
                "r2": round(ya_r["r2"], 5),
                "side": "right",
                "title": titles.get("right"),
                "range": [float(ya_r["v"].min()), float(ya_r["v"].max())],
            }
        # dual-axis: ask per emitted series unless hinted
        if ctype == "line":
            curves = extract_line(clouds, panel, xa, ya, max_points)
            good = [c for c in curves if not c["multivalued"]]
            bad = [c for c in curves if c["multivalued"]]
            good.sort(key=lambda c: c["points"][0] if c.get("points") else [0, 0])
            for c in good:
                c["resolved_by"] = "geometry"
                c.setdefault("label", None)
                c.setdefault("axis", y_side)
            if good:
                # populate label from legend matching for EVERY curve
                # (independent of dual-axis ambiguity) whenever a unique
                # color match exists — display text only, never parsed.
                entries = _legend_entries(page, panel)
                style_counts = collections.Counter(e[0][0] for e in entries)
                entries = [e for e in entries if style_counts[e[0][0]] == 1]
                for c in good:
                    if c.get("label") is None:
                        lab = next(
                            (lab for st, lab in entries if st[0] == c["_style_key"][0]),
                            None,
                        )
                        if lab is not None:
                            c["label"] = lab
            if chart["diagnostics"]["dual_axis"] and good:
                # collect would-be questions for curves the caller has not
                # already answered via hints
                panel_questions = []
                for ci, c in enumerate(good):
                    akey = f"p{pi}.s{ci}.axis"
                    if akey in hints:
                        used_hint_keys.add(akey)
                        continue
                    series_style = _style_dict(c["_style_key"])
                    panel_questions.append(
                        {
                            "id": akey,
                            "chart_id": f"p{pi}",
                            "kind": "y_axis_for_curve",
                            "series_style": series_style,
                            "options": ["left", "right"],
                        }
                    )
                # tier-2: try to answer those questions from the page's own
                # text (legend + rotated axis title) before falling back to
                # the caller-hint tier
                text_answers: dict[str, str] = {}
                if panel_questions:
                    text_answers, labels = resolve_semantics(
                        page, panel, good, panel_questions
                    )
                    for s_idx, lab in labels.items():
                        good[s_idx]["label"] = lab
                local_hints = dict(hints)
                local_hints.update(text_answers)
                for ci, c in enumerate(good):
                    akey = f"p{pi}.s{ci}.axis"
                    if akey in local_hints:
                        resolved_by = "text" if akey in text_answers else "hint"
                        if local_hints[akey] == "right" and panel["ya_right"]:
                            ya2 = panel["ya_right"]
                            # re-extract this curve against right axis
                            cl = {c["_style_key"]: clouds[c["_style_key"]]}
                            c2 = extract_line(cl, panel, xa, ya2, max_points)
                            if c2 and not c2[0]["multivalued"]:
                                c["points"] = c2[0]["points"]
                                c["axis"] = "right"
                                c["resolved_by"] = resolved_by
                        else:
                            c["axis"] = "left"
                            c["resolved_by"] = resolved_by
                    else:
                        # still open — text tier did not produce a unique
                        # answer for this curve. Never leave a numeric table
                        # calibrated against the default left axis sitting on
                        # an axis-unresolved curve: that is a wrong-table
                        # escape path. Drop "points" and "resolved_by"
                        # (both were provisionally set against the default
                        # axis above) and mark the curve as pending so the
                        # caller can correlate it to the open question.
                        q = next(q for q in panel_questions if q["id"] == akey)
                        res["questions"].append(q)
                        c.pop("points", None)
                        c["resolved_by"] = None
                        c["axis"] = None
                        c["pending_question"] = akey
            chart["curves"] = good
            for c in good:
                if c.get("n_extrema_dropped"):
                    chart["diagnostics"]["notes"].append(
                        f"{c['n_extrema_dropped']} local extrema exceeded "
                        f"max_points={max_points}; table simplified — raise "
                        "max_points or read the render for peak questions"
                    )
            if bad:
                chart["diagnostics"]["declined_multivalued"] = len(bad)
            if not good:
                chart["chart_type"] = "declined"
                chart["decline_reason"] = (
                    "all line clouds multivalued " "(crossing/overlapping curves)"
                )
                chart["diagnostics"]["notes"].append(chart["decline_reason"])
        elif ctype == "bar":
            chart["bars"] = extract_bar(bar_rects, xa, ya)
            for s in chart["bars"]:
                s["label"] = None
                s["axis"] = y_side
                s["resolved_by"] = "geometry"
                s["multivalued"] = False
                s["downsampled"] = False
                s["n_extrema_dropped"] = 0
        elif ctype == "scatter":
            chart["points"] = extract_scatter(small_paths, xa, ya)
            for s in chart["points"]:
                s["label"] = None
                s["axis"] = y_side
                s["resolved_by"] = "geometry"
                s["multivalued"] = False
                s["downsampled"] = False
                s["n_extrema_dropped"] = 0
        else:
            chart["chart_type"] = "unknown"
            res["questions"].append(
                {
                    "id": tkey,
                    "chart_id": f"p{pi}",
                    "kind": "chart_type",
                    "options": ["line", "bar", "scatter", "not_a_chart"],
                }
            )
        # out-of-axis-range gate: drop series whose values fall outside the
        # tick range (catches marginal-distribution bars, margin decorations)
        xr, yr = _range(xa), _range(ya)
        yr_right = _range(panel["ya_right"]) if panel["ya_right"] else yr
        dropped = 0
        if "curves" in chart:
            kept = []
            for c in chart["curves"]:
                cyr = yr_right if c.get("axis") == "right" else yr
                if c.get("points") and not in_range_series(c["points"], xr, cyr):
                    dropped += 1
                    continue
                kept.append(c)
            chart["curves"] = kept
        for fld in ("bars", "points"):
            if fld in chart:
                key = "bars" if fld == "bars" else "points"
                kept = []
                for s in chart[fld]:
                    pts = s.get(key) or s.get("points")
                    if pts and not in_range_series(pts, xr, yr):
                        dropped += 1
                        continue
                    kept.append(s)
                chart[fld] = kept
        if dropped:
            chart["diagnostics"]["dropped_out_of_range"] = dropped
        if dropped and not (
            chart.get("curves") or chart.get("bars") or chart.get("points")
        ):
            chart["chart_type"] = "declined"
            chart["decline_reason"] = (
                "series fell outside axis range " "(likely not a data chart)"
            )
            chart.setdefault("diagnostics", {}).setdefault("notes", [])
            chart["diagnostics"]["notes"].append(chart["decline_reason"])
        # final style-shape conversion: internal "_style_key" tuples (used
        # above for color matching / re-extraction) become the public
        # uniform style dict; never leak the internal key.
        for c in chart.get("curves", []):
            c["style"] = _style_dict(c.pop("_style_key"))
        for s in chart.get("bars", []):
            s["style"] = _style_dict(s.pop("_style_key"))
        for s in chart.get("points", []):
            s["style"] = _style_dict(s.pop("_style_key"))
        res["charts"].append(chart)
    unconsumed = set(hints) - used_hint_keys
    if unconsumed:
        return {"error": f"unknown hint id: {sorted(unconsumed)[0]}"}
    if res["questions"]:
        res["status"] = "needs_hint"
    emitted = any(
        c.get("curves") or c.get("bars") or c.get("points") for c in res["charts"]
    )
    if not emitted and not res["questions"]:
        res["status"] = "declined"
        if not res["reasons"]:
            res["reasons"].append("no extractable series passed gates")
    return res


# ---------------- annotated hint renders (halo overlay) ----------------

# translucent halo colors used to highlight a queried series in a hint
# render; must stay visually distinct from common matplotlib series colors
# (tab:blue, tab:red, tab:green, ...) so the halo never blends into the line
# it is meant to point at.
_HALOS: dict[str, tuple[float, float, float]] = {
    "magenta": (1, 0, 1),
    "orange": (1, 0.6, 0),
    "cyan": (0, 0.8, 1),
    "green": (0.1, 0.8, 0.1),
}
_HALO_NAMES: list[str] = list(_HALOS)


def _pick_halo(series_color: tuple[float, ...] | None) -> str:
    """Halo hue that contrasts with the series' own color: maximize
    channel-wise distance."""
    sc = series_color or (0, 0, 0)
    return max(_HALOS, key=lambda n: sum(abs(a - b) for a, b in zip(_HALOS[n], sc)))


def annotate_questions(
    doc: Any,
    page_num: int,
    result: dict[str, Any],
    out_dir: Path,
    pdf_hash: str,
) -> dict[str, str]:
    """Render one annotated clip per panel that has open questions; a
    translucent wide halo is drawn OVER each queried series so the vision
    agent identifies the series by highlight hue.

    Sets ``q["render_path"]`` and ``q["highlight"]`` on every question in
    ``result["questions"]`` and returns ``{chart_id: png_path}``.
    """
    out: dict[str, str] = {}
    by_panel: dict[str, list[dict[str, Any]]] = {}
    for q in result.get("questions", []):
        by_panel.setdefault(q["chart_id"], []).append(q)
    if not by_panel:
        return out
    src_page = doc[page_num]
    panels = find_panels(src_page)
    draws = src_page.get_drawings()
    for chart_id, qs in by_panel.items():
        pi = int(chart_id[1:])
        if pi >= len(panels):
            continue
        panel = panels[pi]
        tmp = pymupdf.open()
        tmp.insert_pdf(doc, from_page=page_num, to_page=page_num)
        page = tmp[0]
        shape = page.new_shape()
        masks = legend_masks(src_page, panel)
        _, _, _, clouds = collect(draws, panel, masks)
        for q in qs:
            series_style = q.get("series_style")
            col = series_style.get("color") if series_style else None
            target = tuple(col) if col else None
            hue = _pick_halo(target)
            q["highlight"] = hue
            if target is not None:
                for style, pts in clouds.items():
                    if style[0] == target and pts:
                        seq = sorted(pts)[:400]
                        shape.draw_polyline(seq)
                        # wide translucent band: ~4x the series' own stroke
                        # width, low opacity, so the thin opaque stroke stays
                        # readable through the halo
                        w = 4.0 * float((series_style or {}).get("width") or 1.0)
                        shape.finish(
                            color=_HALOS[hue],
                            width=max(w, 4.0),
                            stroke_opacity=0.35,
                        )
                        break
        # overlay=True: the halo must go ON TOP of existing content.
        # overlay=False (under) buries it beneath the chart's opaque white
        # plot-background rectangle (matplotlib paints one over the whole
        # figure), making the halo invisible. A wide low-opacity band over a
        # thin opaque stroke keeps the stroke's trajectory clearly readable,
        # which is the actual cue the vision agent needs.
        shape.commit(overlay=True)
        clip = pymupdf.Rect(
            panel["rx0"] - 5,
            panel["ry0"] - 5,
            panel["rx1"] + 5,
            panel["ry1"] + 5,
        )
        pix = page.get_pixmap(dpi=200, clip=clip)
        path = str(out_dir / f"chart_hints_{pdf_hash}_p{page_num + 1}_{chart_id}.png")
        pix.save(path)
        tmp.close()
        for q in qs:
            q["render_path"] = path
        out[chart_id] = path
    return out


def detect_charts_signal(page: Any, budget_ms: int = 250) -> int | None:
    """Cheap discovery: number of chart panels on the page, None if the
    time budget is exhausted (pathological vector soups can take ~700ms;
    None means UNKNOWN, not zero)."""
    import time

    start = time.perf_counter()
    try:
        panels = find_panels(page)
    except Exception:
        return None
    if (time.perf_counter() - start) * 1000 > budget_ms:
        return None
    return len(panels)
