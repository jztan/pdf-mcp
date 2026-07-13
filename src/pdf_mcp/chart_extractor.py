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
"""

import re
import collections
from typing import Any

import numpy as np

CHART_EXTRACTION_VERSION = 1

# a drawing style key: (stroke_color, fill_color, line_width)
Style = tuple[Any, Any, float]

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
            if max(t["cx"] for t in g) - min(t["cx"] for t in g) < 60:
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
            if max(t["cy"] for t in g) - min(t["cy"] for t in g) < 60:
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
            curves.append({"style": k, "multivalued": True})
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
            # local extrema (sign change of dy differences), ranked by
            # prominence = |y - mean of neighbors|
            d1 = np.diff(dy)
            ext = np.where(np.sign(d1[:-1]) * np.sign(d1[1:]) < 0)[0] + 1
            prom = np.abs(dy[ext] - (dy[ext - 1] + dy[ext + 1]) / 2)
            ranked = ext[np.argsort(prom)[::-1]]
            keep_ext = ranked[: max(0, max_points - 2)]
            n_extrema_dropped = max(0, len(ext) - len(keep_ext))
            uniform = np.linspace(0, len(dx) - 1, max_points - len(keep_ext)).astype(
                int
            )
            sel = np.unique(np.concatenate([keep_ext, uniform, [0, len(dx) - 1]]))
            downsampled = True
        curves.append(
            {
                "style": k,
                "multivalued": False,
                "downsampled": downsampled,
                "n_extrema_dropped": int(n_extrema_dropped),
                "points": [[float(f"{dx[i]:.5g}"), float(f"{dy[i]:.5g}")] for i in sel],
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
                [float(f"{apply_ax(xa, cx):.5g}"), float(f"{apply_ax(ya, r.y0):.5g}")]
            )  # top edge = value
        pts.sort()
        series.append({"style": s, "bars": pts})
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
        pts = [
            [float(f"{apply_ax(xa, cx):.5g}"), float(f"{apply_ax(ya, cy):.5g}")]
            for cx, cy in uniq
        ]
        pts.sort()
        series.append({"style": str(ckey), "marker_size": size, "points": pts})
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
        chart: dict[str, Any] = {
            "chart_id": f"p{pi}",
            "panel": pi,
            "chart_type": ctype,
            "x_axis": {"scale": xa["scale"], "r2": round(xa["r2"], 5)},
            "y_axis": {
                "scale": ya["scale"],
                "r2": round(ya["r2"], 5),
                "side": "left" if panel["ya_left"] else "right",
            },
            "diagnostics": {
                "n_frames": len(frames),
                "n_bar_rects": len(bar_rects),
                "n_marker_groups": len(
                    [1 for v in small_paths.values() if len(v) >= 5]
                ),
                "n_line_clouds": len(clouds),
                "dual_axis": bool(panel["ya_left"] and panel["ya_right"]),
            },
        }
        # dual-axis: ask per emitted series unless hinted
        if ctype == "line":
            curves = extract_line(clouds, panel, xa, ya, max_points)
            good = [c for c in curves if not c["multivalued"]]
            bad = [c for c in curves if c["multivalued"]]
            good.sort(key=lambda c: c["points"][0] if c.get("points") else [0, 0])
            if chart["diagnostics"]["dual_axis"] and good:
                for ci, c in enumerate(good):
                    akey = f"p{pi}.s{ci}.axis"
                    series_style = {
                        "color": list(c["style"][0]) if c["style"][0] else None,
                        "width": c["style"][2],
                    }
                    if akey in hints:
                        if hints[akey] == "right" and panel["ya_right"]:
                            ya2 = panel["ya_right"]
                            # re-extract this curve against right axis
                            cl = {c["style"]: clouds[c["style"]]}
                            c2 = extract_line(cl, panel, xa, ya2, max_points)
                            if c2 and not c2[0]["multivalued"]:
                                c["points"] = c2[0]["points"]
                                c["axis"] = "right"
                        else:
                            c["axis"] = "left"
                    else:
                        res["questions"].append(
                            {
                                "id": akey,
                                "kind": "y_axis_for_curve",
                                "series_style": series_style,
                                "options": ["left", "right"],
                            }
                        )
            chart["curves"] = good
            chart.setdefault("diagnostics", {}).setdefault("notes", [])
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
        elif ctype == "bar":
            chart["bars"] = extract_bar(bar_rects, xa, ya)
        elif ctype == "scatter":
            chart["points"] = extract_scatter(small_paths, xa, ya)
        else:
            chart["chart_type"] = "unknown"
            res["questions"].append(
                {
                    "id": tkey,
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
        res["charts"].append(chart)
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
