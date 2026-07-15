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
import math
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf

CHART_EXTRACTION_VERSION = 11


def hints_hash(hints: dict[str, str] | None) -> str:
    """Stable short digest of a hints dict, order-independent. ``None`` and
    ``{}`` hash identically — both mean "no hints" for cache-key purposes."""
    return hashlib.sha1(json.dumps(hints or {}, sort_keys=True).encode()).hexdigest()[
        :16
    ]


# a drawing style key: (stroke_color, fill_color, line_width)
Style = tuple[Any, Any, float, Any]


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
    dash = None
    if len(style_key) >= 3:
        color, width = style_key[0], style_key[2]
        if len(style_key) >= 4:
            dash = style_key[3]
    else:
        color, width = style_key
    return {
        "color": list(color) if color else None,
        "width": float(width),
        "dash": dash,
    }


# ---------------- text/tick helpers (from v2, proven) ----------------


def get_words(page: Any) -> Any:
    return page.get_text("words")


def _power_pairs(
    page: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair ``base^exponent`` tick labels; also report the bases that failed.

    Returns (paired, orphan_bases). ``paired`` entries are the recovered
    powers (see superscript_powers). ``orphan_bases`` are '10'/'2' spans that
    paired with nothing — meaningless globally (every linear-axis '10' tick
    and body-text '2' lands here), but meaningful to the per-axis unreadable-
    ticks guard, which only consults them when one sits immediately left of a
    calibrated tick at raised-exponent geometry.
    """
    out: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    # stage-3 drawn-minus reading: matplotlib mathtext (and some journal
    # typesetters) draw the exponent's minus as a RULE, not a glyph. The
    # bar's presence in the base->exponent gap at superscript height is a
    # precise signal (0 false positives corpus-wide as a decline trigger;
    # see RESULTS.md v6-v8) — precise enough to READ: negate the exponent.
    bars = _hrule_bars(page.get_drawings())
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
        # base: restricted to the log bases that actually occur on chart axes
        # (10 and 2). Allowing arbitrary bases created false matches from
        # coincidental adjacencies of unrelated numbers on dense multi-panel
        # figures ("9^2", "20^8"), corrupting calibration — verified on
        # 2605.06546 p20. A base-16/base-e chart would be a sample-driven add.
        if a["t"] not in ("10", "2"):
            continue
        paired = False
        for b in spans:
            if b is a or not re.fullmatch(r"-?\d{1,2}", b["t"]):
                continue
            gap = b["bb"][0] - a["bb"][2]
            geom_ok = (
                b["size"] < 0.85 * a["size"]
                # gap lower bound is -2, not 0: renderers kern the raised
                # exponent to slightly OVERLAP the base (SGDR 1608.03983:
                # exp.x0 - base.x1 = -0.007), and a 0-floor rejects the pair,
                # leaving orphaned exponents that calibrate as a bogus linear
                # axis ("Learning rate" 10^-4..10^0 emitted as [-4, 0]).
                and -2 <= gap < 9
                and b["bb"][1] < a["bb"][1] + 0.5
                # vertical bands must OVERLAP: a superscript sits beside its
                # base (top raised, bottom still below the base's top). The
                # -2 x-overlap alone let an x-tick pair with unrelated text
                # 88pt below it (2607.08500 p25: tick "-3" + a body-text "2"
                # -> bogus 2^-3 that ate the tick and broke the axis).
                and b["bb"][3] > a["bb"][1]
            )
            if geom_ok:
                # drawn-minus test: a thin bar in the base->exponent gap at
                # superscript height (strictly below the exponent's top edge
                # +0.2 — a grazing error-bar cap 0.2pt ABOVE the bbox must
                # not negate a positive tick; 2607.06360 p20). Only for
                # unsigned exponents: a typed minus is already in b["t"].
                exp_txt = b["t"]
                mid_y = (b["bb"][1] + b["bb"][3]) / 2
                negated = False
                if not exp_txt.startswith("-"):
                    negated = any(
                        a["bb"][2] - 0.6 < bx < b["bb"][0] + 0.6
                        and b["bb"][1] + 0.2 < by < mid_y + 1.0
                        for bx, by in bars
                    )
                if gap >= 3 and not negated:
                    # a wide gap is only a superscript pair when the drawn
                    # minus explains the space (A&A family, gap ~4.6pt);
                    # otherwise adjacency is coincidental — do not pair.
                    continue
                val = (
                    float(a["t"]) ** -float(exp_txt)
                    if negated
                    else float(a["t"]) ** float(exp_txt)
                )
                x0, y0 = a["bb"][0], min(a["bb"][1], b["bb"][1])
                x1, y1 = b["bb"][2], max(a["bb"][3], b["bb"][3])
                out.append(
                    {
                        "v": val,
                        "cx": (x0 + x1) / 2,
                        "cy": (y0 + y1) / 2,
                        "bb": (x0, y0, x1, y1),
                        "raw": f"{a['t']}^{'-' if negated else ''}{exp_txt}",
                    }
                )
                paired = True
                break
        if not paired:
            orphans.append(a)
    return out, orphans


def superscript_powers(page: Any) -> list[dict[str, Any]]:
    """Recover ``base^exponent`` tick labels typeset with a raised superscript.

    Handles bases 10 and 2 (see the base filter in _power_pairs) — powers-of-
    two axes (batch size, sequence length, dataset size) are ubiquitous in ML
    and, read as plain text, `2^19` glues to "219", which then fits a *linear*
    axis at r2=1.0 and silently emits a table off by orders of magnitude and
    wrong in scale type (verified wrong-emit on Hestness 1712.00409 Fig 1).
    The base and exponent are separate spans (exponent smaller + baseline-
    raised), so `b^k` is recoverable geometrically the same way `10^k` is.
    """
    return _power_pairs(page)[0]


def numeric_tokens(page: Any) -> list[dict[str, Any]]:
    sup = superscript_powers(page)
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
    # dv-uniformity floor must be SCALE-AWARE: with an absolute 1e-9 floor,
    # any micro-magnitude tick set (astro fluxes 1e-15..1e-11) trivially
    # passes as "uniform" and calibrates LINEAR on a log axis — interpolated
    # values silently wrong (caught adjudicating 2607.06360 p19 after the
    # drawn-minus reader unlocked it).
    _floor = max(1e-9 * float(np.abs(v).max()), 1e-30)
    if abs(dv.max() - dv.min()) <= 0.25 * max(abs(dv.mean()), _floor):
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
            "toks": g,
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
                "toks": g,
            }
    return None


def apply_ax(ax: dict[str, Any], p: Any) -> Any:
    val = ax["a"] * np.asarray(p, float) + ax["b"]
    return 10 ** val if ax["scale"] == "log" else val


# ---------------- drawing helpers ----------------


def _dash_key(d: dict[str, Any]) -> str | None:
    """Normalized dash pattern, None for solid. Part of the style key: a
    solid data curve and its same-color DASHED power-law fit are different
    series — keyed without the dash they merged into one interleaved
    "sawtooth" chimera that traced neither real curve and, when the fit ran
    past the axis corner, dragged the whole panel into an out-of-range
    decline (consumer-found on Henighan Fig 16). Numbers are rounded so
    float noise between segments of one curve cannot split it."""
    raw = d.get("dashes")
    if not raw or raw in ("[] 0", "[]0"):
        return None
    nums = re.findall(r"-?\d+(?:\.\d+)?", str(raw))
    if not nums or all(float(n) == 0 for n in nums):
        return None
    return " ".join(f"{float(n):.1f}" for n in nums[:4])


def draw_style(d: dict[str, Any]) -> Style:
    stroke = tuple(round(x, 2) for x in d["color"]) if d.get("color") else None
    fill = tuple(round(x, 2) for x in d["fill"]) if d.get("fill") else None
    return (stroke, fill, round(d.get("width") or 0, 2), _dash_key(d))


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


def _word_lines(page: Any, panel: dict[str, Any]) -> list[list[Any]]:
    """In-panel non-numeric words grouped into baseline lines (shared by
    legend_masks and _legend_entries)."""
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
        return []
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
    return lines


def legend_masks(
    page: Any, panel: dict[str, Any]
) -> list[tuple[float, float, float, float]]:
    """Geometry masks over LEGEND rows only — not every in-panel label.

    Masking every non-numeric word blanketed text-dense panels (EfficientNet
    Fig 1: ~15 point annotations + an inset table produced 72 masks covering
    135% of the panel area) and per-vertex masking then ATE the data curves —
    the root cause of the composite-figure empty class. Masks exist to keep
    legend samples/markers out of collected geometry, so a mask now requires
    a legend SIGNATURE:
      - a short thin STROKE sample immediately left of the text line
        (line-chart legends — the class that pollutes clouds), or
      - a small marker glyph immediately left, on >=2 vertically-adjacent
        left-aligned lines (scatter legends; a single label with a marker
        nearby is a point annotation, not a legend).
    Annotation text itself never pollutes geometry — masks only ever filter
    drawings — so unmasked labels are harmless.
    """
    lines = _word_lines(page, panel)
    if not lines:
        return []
    dboxes = []
    for d in page.get_drawings():
        bb = d_bbox(d)
        if bb is not None:
            dboxes.append((d, bb))
    boxes = [bb for _d, bb in dboxes]

    info = []  # ((x0,y0,x1,y1), has_stroke_sample, marker_offset|None)
    for ln in lines:
        x0 = ln[0][0]
        y0 = min(w[1] for w in ln)
        x1 = max(w[2] for w in ln)
        y1 = max(w[3] for w in ln)
        stroke = False
        marker_off: float | None = None
        for bb in boxes:
            if not (
                x0 - 48 <= bb[0]
                and bb[2] <= x0 - 2
                and bb[1] >= y0 - 4
                and bb[3] <= y1 + 4
            ):
                continue
            w_, h_ = bb[2] - bb[0], bb[3] - bb[1]
            if w_ >= 8 and h_ <= (y1 - y0) + 8:
                stroke = True
            elif 1.5 <= max(w_, h_) < 8:
                marker_off = x0 - bb[0]
        info.append(((x0, y0, x1, y1), stroke, marker_off))

    # small-glyph style census (color+size bucket) for the lone-row rule
    glyph_counts: dict[tuple[Any, int], int] = {}
    for d_, bb in dboxes:
        w_, h_ = bb[2] - bb[0], bb[3] - bb[1]
        if 1.5 <= max(w_, h_) < 8 and 0.35 <= (w_ + 1e-6) / (h_ + 1e-6) <= 2.8:
            st = draw_style(d_)
            key = (st[1] or st[0], round(max(w_, h_)))
            glyph_counts[key] = glyph_counts.get(key, 0) + 1
    row_glyph_key: list[tuple[Any, int] | None] = []
    for ln in lines:
        x0 = ln[0][0]
        y0 = min(w[1] for w in ln)
        y1 = max(w[3] for w in ln)
        gk = None
        for d_, bb in dboxes:
            if (
                x0 - 48 <= bb[0]
                and bb[2] <= x0 - 2
                and bb[1] >= y0 - 4
                and bb[3] <= y1 + 4
            ):
                w_, h_ = bb[2] - bb[0], bb[3] - bb[1]
                if 1.5 <= max(w_, h_) < 8:
                    st = draw_style(d_)
                    gk = (st[1] or st[0], round(max(w_, h_)))
        row_glyph_key.append(gk)

    masks: list[tuple[float, float, float, float]] = []
    for i, (bb, stroke, marker_off) in enumerate(info):
        keep = stroke
        if not keep and marker_off is not None:
            # lone-row rule: a single label whose adjacent marker's style
            # RECURS as panel data (>=5 same-style glyphs) is a legend sample
            # of an emitting series — unmasked it injects a fabricated point
            # into that series (attack D2, single-entry unframed legend). A
            # uniquely-styled labeled point doesn't match and stays.
            gk = row_glyph_key[i]
            if gk is not None and glyph_counts.get(gk, 0) >= 5:
                keep = True
        if not keep and marker_off is not None:
            # marker-legend rule: needs a left-aligned vertical neighbor row
            # whose sample sits at the SAME label offset — legends stack with
            # consistent sample indents; stacked point ANNOTATIONS (r=1.3 /
            # r=1.5 beside a rising curve, EfficientNet Fig 3) have their
            # data markers at varying offsets and must not mask.
            lh = bb[3] - bb[1]
            for j, (bb2, s2, m2) in enumerate(info):
                if j == i:
                    continue
                if s2 is False and m2 is None:
                    continue
                stacked = abs(bb2[0] - bb[0]) <= 10 and abs(bb2[1] - bb[1]) <= (
                    2.5 * max(lh, 6)
                )
                same_row = abs(bb2[1] - bb[1]) < 3  # ncol legends sit on one baseline
                if not (stacked or same_row):
                    continue
                if m2 is not None and abs(m2 - marker_off) > 6:
                    continue
                keep = True
                break
        if keep:
            masks.append((bb[0] - 45, bb[1] - 3, bb[2] + 3, bb[3] + 3))
    # framed-legend rule: a compact sub-panel box enclosing >=2 label rows
    # is a legend even when its per-row samples defeat the strip geometry
    # (some renderers draw all samples as one path — 2607.09566 p30's
    # unmasked samples merged into data clouds and killed the panel as
    # "multivalued"). Mask every row inside such a box.
    pw = panel["rx1"] - panel["rx0"]
    ph = panel["ry1"] - panel["ry0"]
    frame_boxes: list[tuple[float, float, float, float]] = []
    for d_, bb in dboxes:
        if d_.get("color") is None:
            # fill-only rect = shaded region (axvspan); treating one as a
            # legend frame masked the annotations inside it AND their 45pt
            # strips clipped a real curve crest (attack D3). Legend frames
            # are stroked.
            continue
        w_, h_ = bb[2] - bb[0], bb[3] - bb[1]
        if w_ * h_ > 0.35 * pw * ph or w_ > 0.8 * pw or h_ > 0.8 * ph:
            continue
        if w_ < 12 or h_ < 8:
            continue
        inside = [
            i
            for i, (lb, _s, _m) in enumerate(info)
            if bb[0] <= (lb[0] + lb[2]) / 2 <= bb[2]
            and bb[1] <= (lb[1] + lb[3]) / 2 <= bb[3]
        ]
        if len(inside) >= 1:
            frame_boxes.append(bb)
            for i in inside:
                lb = info[i][0]
                m = (lb[0] - 45, lb[1] - 3, lb[2] + 3, lb[3] + 3)
                if m not in masks:
                    masks.append(m)

    # mask legend-frame BORDERS (thin bands only): the box stroke around the
    # entries otherwise enters clouds as a fabricated 10-18 vertex "curve" at
    # legend position (2607.09566 p27 grey artifact; single-entry framed
    # legends emitted their own frame as THE curve — adversarial attack D1).
    # Bands, NOT interiors — interior masking ate curves near legends. Bands
    # apply to frame_boxes regardless of whether any row masks exist, and
    # ONLY to drawings whose own vertices hug their bbox perimeter: a frame's
    # path lies on its border; a data curve fills its bbox interior (banding
    # arbitrary bboxes ate a curve's own apex/baseline — attack D4).
    def _perimeter_hugging(d: dict[str, Any], bb: Any) -> bool:
        pts = path_pts(d)
        if len(pts) < 4:
            return False
        near = sum(
            1
            for x, y in pts
            if min(abs(x - bb[0]), abs(x - bb[2])) < 2.5
            or min(abs(y - bb[1]), abs(y - bb[3])) < 2.5
        )
        return near >= 0.9 * len(pts)

    row_centers = [((lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2) for lb, _s, _m in info]
    bordered: list[tuple[float, float, float, float]] = []
    for d in page.get_drawings():
        bb = d_bbox(d)
        if bb is None:
            continue
        w_, h_ = bb[2] - bb[0], bb[3] - bb[1]
        if w_ * h_ > 0.35 * pw * ph or w_ > 0.8 * pw or h_ > 0.8 * ph:
            continue
        if w_ < 12 or h_ < 8:
            continue
        if not any(
            bb[0] <= cx <= bb[2] and bb[1] <= cy <= bb[3] for cx, cy in row_centers
        ):
            continue
        if _perimeter_hugging(d, bb):
            bordered.append(bb)
    B = 1.5
    for bb in bordered:
        masks.append((bb[0] - B, bb[1] - B, bb[2] + B, bb[1] + B))  # top
        masks.append((bb[0] - B, bb[3] - B, bb[2] + B, bb[3] + B))  # bottom
        masks.append((bb[0] - B, bb[1] - B, bb[0] + B, bb[3] + B))  # left
        masks.append((bb[2] - B, bb[1] - B, bb[2] + B, bb[3] + B))  # right
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
    lines = _word_lines(page, panel)
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
    # Anchor on the y-tick COLUMN geometry, not the (frame-refined) panel box:
    # frame refinement can enclose the axis titles, moving the box edges past
    # them. Tick positions are stable. Use ya_left specifically (NOT a fallback
    # to the primary ya, which on a right-only panel IS the right axis — that
    # would let a right-side title spuriously populate out["left"]).
    ya_l = panel.get("ya_left")
    ya_r = panel.get("ya_right")
    if ya_l is not None:
        y_top = float(ya_l["px"].min())
        y_bot = float(ya_l["px"].max())
    else:
        y_top, y_bot = panel["ry0"], panel["ry1"]
    # A y-axis title sits IMMEDIATELY beside its own tick column. On a tight
    # multi-panel figure a neighbor panel's title is left of this panel's tick
    # column too, so an unbounded "left of the column" test steals it (verified:
    # Chinchilla's "Tokens" panel got the center panel's "Parameters" title).
    # Fix: cap the horizontal gap and keep the NEAREST candidate to this
    # column, not the last one iterated.
    MAX_GAP = 55.0
    best_l: tuple[float, str] | None = None
    best_r: tuple[float, str] | None = None
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            if abs(line.get("dir", (1, 0))[0]) > 0.3:
                continue  # not vertical text
            bb = line["bbox"]
            if bb[3] < y_top - 30 or bb[1] > y_bot + 30:
                continue  # not vertically alongside the axis
            text = re.sub(
                r"\s+", " ", " ".join(s["text"] for s in line["spans"]).strip()
            )
            if not text or not _looks_like_axis_title(text):
                continue
            if ya_l is not None:
                gap = ya_l["x_at"] - bb[2]  # title is left of the tick column
                if 2 <= gap <= MAX_GAP and (best_l is None or gap < best_l[0]):
                    best_l = (gap, text)
            if ya_r is not None:
                gap = bb[0] - ya_r["x_at"]  # title is right of the tick column
                if 2 <= gap <= MAX_GAP and (best_r is None or gap < best_r[0]):
                    best_r = (gap, text)
    if best_l is not None:
        out["left"] = best_l[1]
    if best_r is not None:
        out["right"] = best_r[1]
    return out


def _x_axis_title(page: Any, panel: dict[str, Any]) -> str | None:
    """x-axis title: horizontal text centered under the tick-label row,
    within a plausible band below the panel. Returns the nearest such line,
    or None (display string only — never parsed as data)."""
    # Anchor on the x-tick label row (xa["y_at"]) and the tick span, not the
    # frame-refined panel box (which can enclose the title, pushing ry1 below
    # it). The predicate — not a tight band — is what rejects body-text/caption
    # pollution, so the band can be generous and the nearest passing line wins.
    xa = panel.get("xa")
    if xa is None:
        return None
    y_row = float(xa["y_at"])
    x0t, x1t = float(xa["px"].min()), float(xa["px"].max())
    cx_target = (x0t + x1t) / 2
    span = max(x1t - x0t, 1e-6)
    d = page.get_text("dict")
    best: tuple[float, str] | None = None
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            if abs(line.get("dir", (1, 0))[1]) > 0.3:
                continue  # not horizontal text
            bb = line["bbox"]
            if not (y_row + 2 <= bb[1] <= y_row + 45):
                continue  # not just below the tick-label row
            text = re.sub(
                r"\s+", " ", " ".join(s["text"] for s in line["spans"]).strip()
            )
            if not text or not _looks_like_axis_title(text):
                continue
            line_cx = (bb[0] + bb[2]) / 2
            if abs(line_cx - cx_target) > 0.35 * span:
                continue
            dist = bb[1] - y_row
            if best is None or dist < best[0]:
                best = (dist, text)
    return best[1] if best else None


def _hrule_bars(draws: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Centers of thin short horizontal filled bars in the page drawings.

    matplotlib's mathtext draws the MINUS of a superscript exponent as a rule
    (a filled bar), not a glyph — `10^-6` has zero minus characters in the
    text layer (Henighan 2010.14701 Fig 16). These bars are how that
    invisible sign is detected.
    """
    bars: list[tuple[float, float]] = []
    for d in draws:
        xs: list[float] = []
        ys: list[float] = []
        for it in d["items"]:
            for p in it[1:]:
                if hasattr(p, "x"):
                    xs.append(p.x)
                    ys.append(p.y)
                elif hasattr(p, "x0"):
                    xs += [p.x0, p.x1]
                    ys += [p.y0, p.y1]
        if not xs:
            continue
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if 0.6 < w < 4.5 and h < 1.0:
            bars.append(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
    return bars


def _ticks_unreadable(
    ax: dict[str, Any],
    bars: list[tuple[float, float]],
    orphan_bases: list[dict[str, Any]],
) -> str | None:
    """Return a reason when an axis's tick labels look like base^exponent
    typography the reader could not resolve — the calibration would be wrong
    in sign or scale. None when the ticks are trustworthy.

    Two per-axis signals (scoped on real wrong-emits, see RESULTS.md):

    - vector-minus: a recovered `10^k` tick has an hrule bar inside its bbox
      at superscript height. The exponent's minus was drawn, not typed, so
      the recovered value is silently positive (`10^-6` -> `10^6`); ONE such
      tick already falsifies the axis.
    - orphan exponent: a LINEAR calibration where the ticks sit immediately
      right of a larger unpaired '10'/'2' span at raised-exponent geometry —
      the "values" are exponents whose base failed to pair (kerning/size
      quirks beyond what the pairing gate accepts).
    """
    toks = ax.get("toks") or []
    if not toks:
        return None
    for t in toks:
        raw = t.get("raw", "")
        # only unsigned recovered powers: a parsed minus ("10^-12") is PROOF
        # the sign was typed — a drawn minus leaves zero minus glyphs, so the
        # vector-minus signal cannot apply (falsely declined 1406.6799 p7,
        # whose typed 10^-12..10^-9 axes read correctly).
        if "^" not in raw or "^-" in raw:
            continue
        bb = t["bb"]
        mid_y = (bb[1] + bb[3]) / 2
        if any(
            # the bar must sit strictly INSIDE the tick's own bbox at
            # superscript height — Henighan's drawn minus is ~2pt below the
            # bbox top. Without the lower bound, dashed curves / minor tick
            # marks far ABOVE the label (same x-column) falsely declined
            # all-positive axes (2010.14701 p12 Fig 7, 2203.15556 p23); with
            # a -0.5pt slack, an error-bar cap grazing the bbox top from the
            # plot above still false-declined 2607.06360 p20 (bar 0.2pt above
            # the top edge). A real drawn minus is centered on the exponent,
            # never above the merged bbox top.
            bb[0] - 0.5 < bx < bb[2] + 0.5 and bb[1] + 0.2 < by < mid_y + 0.5
            for bx, by in bars
        ):
            return (
                "tick label sign is drawn, not typed (vector minus on a "
                "superscript exponent) — axis sign unreadable"
            )
    if ax["scale"] == "linear":
        n_exp = 0
        for t in toks:
            bb = t["bb"]
            h = bb[3] - bb[1]
            for b in orphan_bases:
                obb = b["bb"]
                if (
                    # window reaches 8pt: a VECTOR-DRAWN minus occupies
                    # ~3-5pt between base and exponent (2607.06844 SED plot:
                    # gap 4.6pt — the exponents orphaned AND escaped this
                    # guard at <4, emitting a linear [1,11] axis for a
                    # 10^-11..10^5 log scale)
                    -4 <= bb[0] - obb[2] < 8  # immediately right of the base
                    and (obb[3] - obb[1]) > 1.15 * max(h, 1e-6)  # base larger
                    and bb[3] < obb[3] - 1  # tick raised above base bottom
                    and bb[1] < obb[3]
                    and bb[3] > obb[1]  # vertical bands overlap
                ):
                    n_exp += 1
                    break
        if n_exp >= 2 and n_exp * 2 >= len(toks):
            return (
                "tick labels are raised exponents of an unpaired '10'/'2' "
                "base — axis is a mis-read log scale"
            )
    return None


def _title_says_log(title: str | None) -> bool:
    """True when an axis title DECLARES a log scale — matches "log scale",
    "log-scale", "logarithmic", "(log)". Deliberately does NOT match a logged
    QUANTITY like "log likelihood"/"log loss" (those are values plotted on a
    possibly-linear axis). Used as a contradiction guard: title says log but
    calibration came out linear ⇒ the tick labels weren't read correctly
    (e.g. an unrecoverable superscript typography) ⇒ decline, never emit a
    mis-scaled linear table."""
    if not title:
        return False
    # \b before "log" so we don't match "anaLOG SCALE" / "cataLOG scale"
    # (Visual Analog Scale is a common *linear* axis — a false decline).
    return bool(
        re.search(r"\blog[\s-]?scale|\blogarithmic|\(\s*log\s*\)", title.lower())
    )


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


def _marker_connected(
    pts: list[tuple[float, float]],
    small_paths: dict[tuple[Any, int], list[tuple[float, float]]],
) -> bool:
    """True when a polyline's vertices coincide with plotted markers.

    A short cloud (a 5-point scaling-law frontier) is indistinguishable from
    decoration by point count alone, but data lines drawn point-per-model
    carry a marker at each vertex — that coincidence is strong evidence the
    polyline is data, not a bracket/arrow/axis break. Requires >=3 vertex-
    marker hits covering >=50% of the distinct vertices."""
    if not small_paths:
        return False
    centers = [c for v in small_paths.values() for c in v]
    if not centers:
        return False
    uniq: list[tuple[float, float]] = []
    for p in pts:
        if not any(abs(p[0] - u[0]) < 0.5 and abs(p[1] - u[1]) < 0.5 for u in uniq):
            uniq.append(p)
    hits = sum(
        1
        for p in uniq
        if any(abs(p[0] - c[0]) < 2.5 and abs(p[1] - c[1]) < 2.5 for c in centers)
    )
    return hits >= 3 and hits * 2 >= len(uniq)


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
    # sparse marker-connected lines: a 3-7 vertex polyline whose vertices
    # carry markers (one point per model size — the canonical scaling-law
    # figure) is a data line even though it fails the dense-cloud gate.
    for k, pts in clouds.items():
        xs = [p[0] for p in pts]
        if (
            len(pts) >= 3
            and max(xs) - min(xs) >= 0.25 * pw
            and _marker_connected(pts, small_paths)
        ):
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
    small_paths: dict[tuple[Any, int], list[tuple[float, float]]] | None = None,
) -> list[dict[str, Any]]:
    pw = panel["rx1"] - panel["rx0"]
    ph = panel["ry1"] - panel["ry0"]
    curves: list[dict[str, Any]] = []
    for k, pts in clouds.items():
        xs = np.array([p[0] for p in pts])
        ys = np.array([p[1] for p in pts])
        if len(pts) < 8 or np.ptp(xs) < 0.25 * pw:
            # sparse-cloud path: a short polyline is still a data line when
            # its vertices carry markers (scaling-law figures plot one point
            # per model). Marker-vertex coincidence is MANDATORY here even
            # when the caller hinted "line": the hint confirms the CHART
            # type, not that every short polyline in the panel is data — a
            # hinted bypass emitted a significance bracket as a curve
            # (adversarial review probe). Span gate stays too:
            # brackets/arrows/axis-break decorations are short-span.
            sparse_ok = (
                len(pts) >= 3
                and np.ptp(xs) >= 0.25 * pw
                and _marker_connected(pts, small_paths or {})
            )
            if not sparse_ok:
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
    min_pts: int = 5,
) -> list[dict[str, Any]]:
    """min_pts=5 filters decoration/annotation markers on the geometry-only
    path; extract_charts lowers it to 3 when the caller EXPLICITLY hinted
    "scatter" — the agent has looked at the render and confirmed the chart
    type, so a 3-point error-bar measurement series may emit. Not lower:
    2-point same-style groups are how paired annotation arrowheads look
    (adversarial review probe), and a hint confirms the chart, not that
    every tiny style group is data."""
    series: list[dict[str, Any]] = []
    for (ckey, size), centers in small_paths.items():
        # merge fill+stroke duplicates drawn at the same location
        uniq: list[tuple[float, float]] = []
        for cx, cy in sorted(centers):
            if not any(abs(cx - ux) < 1.5 and abs(cy - uy) < 1.5 for ux, uy in uniq):
                uniq.append((cx, cy))
        if len(uniq) < min_pts:
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
    xlog: bool = False,
    ylog: bool = False,
) -> bool:
    """keep only series where >=need fraction of points fall within the
    tick range (+/- margin). Marginal-distribution bars / decorations that
    extend into the plot margins map outside the axis range and are dropped."""
    if not pts:
        return False

    def _within(v: float, lo: float, hi: float, is_log: bool) -> bool:
        # log axes need the margin in DECADES: a linear +/-15% margin is
        # microscopic at the top of a log range (a curve legitimately
        # running past the last tick to the panel corner — 0.5 decades on
        # Henighan p22 Text-to-Image — read as "outside" and the whole
        # panel false-declined; consumer-found) and unbounded at the bottom.
        if is_log and lo > 0 and hi > 0:
            if v <= 0:
                return False
            m = frac * max(math.log10(hi) - math.log10(lo), 1e-9)
            return math.log10(lo) - m <= math.log10(v) <= math.log10(hi) + m
        m = frac * max(hi - lo, 1e-9)
        return lo - m <= v <= hi + m

    ok = sum(
        1
        for x, y in pts
        if _within(x, xr[0], xr[1], xlog) and _within(y, yr[0], yr[1], ylog)
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
    # page-level context for the unreadable-ticks guard (computed once)
    _bars = _hrule_bars(draws)
    _, _orphan_bases = _power_pairs(page)
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
        type_hinted = tkey in hints
        if type_hinted:
            ctype = hints[tkey]
            used_hint_keys.add(tkey)
            if ctype == "not_a_chart":
                # terminal answer: the caller looked at the render and said
                # this panel is not a data chart. Decline — do not re-ask
                # (pre-fix this fell into the unknown branch and looped).
                y_side0 = "left" if panel["ya_left"] else "right"
                res["charts"].append(
                    {
                        "chart_id": f"p{pi}",
                        "panel": pi,
                        "chart_type": "declined",
                        "decline_reason": (
                            "caller identified the panel as not a chart"
                        ),
                        "region_bbox": [
                            float(panel["rx0"]),
                            float(panel["ry0"]),
                            float(panel["rx1"]),
                            float(panel["ry1"]),
                        ],
                        "x_axis": {
                            "scale": xa["scale"],
                            "r2": round(xa["r2"], 5),
                            "title": None,
                            "range": [float(xa["v"].min()), float(xa["v"].max())],
                        },
                        "y_axis": {
                            "scale": ya["scale"],
                            "r2": round(ya["r2"], 5),
                            "side": y_side0,
                            "title": None,
                            "range": [float(ya["v"].min()), float(ya["v"].max())],
                        },
                        "diagnostics": {
                            "n_frames": 0,
                            "n_bar_rects": 0,
                            "n_marker_groups": 0,
                            "n_line_clouds": 0,
                            "dual_axis": False,
                            "notes": ["caller identified the panel as not a chart"],
                        },
                    }
                )
                continue
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
        # unreadable-ticks guard: decline when an axis's tick labels are
        # base^exponent typography the reader could not resolve (vector-drawn
        # minus, or orphaned exponents of an unpaired base). The calibration
        # fits at r2~1.0 either way, so without this check the chart emits a
        # confidently-wrong axis (Henighan 2010.14701: 10^-6 read as 10^6).
        _ax_series = [("x", xa), ("y", ya)]
        if panel["ya_left"] and panel["ya_right"]:
            _ax_series.append(("right y", panel["ya_right"]))
        _bad = [
            (name, why)
            for name, s in _ax_series
            for why in [_ticks_unreadable(s, _bars, _orphan_bases)]
            if why
        ]
        if _bad:
            name, why = _bad[0]
            chart["chart_type"] = "declined"
            chart["decline_reason"] = f"{name}-axis: {why}"
            chart["diagnostics"]["notes"].append(chart["decline_reason"])
            res["charts"].append(chart)
            continue

        # contradiction guard: an axis whose TITLE declares a log scale but
        # which calibrated as linear means the tick labels were mis-read
        # (e.g. a base^exp superscript typography we couldn't recover) — the
        # numbers would be off by orders of magnitude. Decline rather than
        # emit a confidently-wrong linear table.
        _axes = [chart["x_axis"], chart["y_axis"]]
        if "y_axis_right" in chart:
            _axes.append(chart["y_axis_right"])
        if any(
            ax["scale"] == "linear" and _title_says_log(ax.get("title")) for ax in _axes
        ):
            chart["chart_type"] = "declined"
            chart["decline_reason"] = (
                "axis title declares a log scale but calibration is linear — "
                "tick labels not read reliably (unrecoverable superscript?)"
            )
            chart["diagnostics"]["notes"].append(chart["decline_reason"])
            res["charts"].append(chart)
            continue

        # no-vector-geometry guard: axes calibrated but the panel interior
        # holds (essentially) no clouds, markers, or bars — either the plot
        # content is rasterized (vector axes framing an image: matplotlib
        # `rasterized=True`, microscopy figures), or the markers use shapes
        # geometry collection doesn't capture. A chart_type question is
        # unanswerable in both cases; decline with the reason instead.
        # "Essentially": <=2 vertices total also counts — phantom panels
        # (spurious axis pairings near a real figure) carry a stray vertex.
        _n_geom = (
            sum(len(v) for v in clouds.values())
            + sum(len(v) for v in small_paths.values())
            + len(bar_rects)
        )
        if _n_geom <= 2:
            chart["chart_type"] = "declined"
            chart["decline_reason"] = (
                "no extractable vector plot geometry inside the panel — "
                "rasterized data (image content) or unsupported marker shapes"
            )
            chart["diagnostics"]["notes"].append(chart["decline_reason"])
            res["charts"].append(chart)
            continue

        # dual-axis: ask per emitted series unless hinted
        if ctype == "line":
            curves = extract_line(clouds, panel, xa, ya, max_points, small_paths)
            # honesty notes for the sparse path (no silent caps): flag short
            # captures, and flag data-like clouds left below the gates so an
            # agent knows the table may be missing a series it can see in
            # the render.
            _pw = panel["rx1"] - panel["rx0"]
            _emitted_styles = {c["_style_key"] for c in curves}
            for c in curves:
                pts = c.get("points") or []
                if 0 < len(pts) < 8:
                    chart["diagnostics"]["notes"].append(
                        f"sparse line capture ({len(pts)} vertices) — "
                        "verify completeness against the render"
                    )
            _left_behind = sum(
                1
                for k, pts in clouds.items()
                if k not in _emitted_styles
                and len(pts) >= 3
                and (max(p[0] for p in pts) - min(p[0] for p in pts)) >= 0.25 * _pw
            )
            if _left_behind and curves:
                chart["diagnostics"]["notes"].append(
                    f"{_left_behind} line cloud(s) below extraction gates "
                    "not emitted — the chart may show more series than the "
                    "table; check the render"
                )
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
                if good:
                    # partial-capture honesty (consumer round 3): on dense
                    # multi-curve figures the separable minority (often a
                    # reference/fit line) emits while the actual result
                    # curves drop as multivalued — a bare "ok" then reads as
                    # "extracted the figure". Say what is missing, loudly.
                    # NB: each dropped item is a same-style CLOUD that may
                    # contain MANY visual curves — never state a curve count.
                    chart["diagnostics"]["notes"].append(
                        f"partial capture: {len(bad)} same-style line "
                        "cloud(s) with multiple overlapping curves could "
                        "not be separated and are NOT in the table — the "
                        "figure likely shows more curves than emitted; "
                        "check the render"
                    )
            if not good and bad:
                chart["chart_type"] = "declined"
                chart["decline_reason"] = (
                    "all line clouds multivalued " "(crossing/overlapping curves)"
                )
                chart["diagnostics"]["notes"].append(chart["decline_reason"])
        elif ctype == "bar":
            chart["bars"] = extract_bar(bar_rects, xa, ya)
            if not chart["bars"] and not type_hinted:
                # geometry said "bar" but no series stood on the baseline —
                # usually large OPEN markers misread as bar rects (astro
                # scatter squares/triangles). The classification is the
                # unreliable part, so fall back to the chart_type question
                # instead of returning a typed-but-empty chart.
                del chart["bars"]
                ctype = "unknown"
                chart["chart_type"] = "unknown"
                res["questions"].append(
                    {
                        "id": tkey,
                        "chart_id": f"p{pi}",
                        "kind": "chart_type",
                        "options": ["line", "bar", "scatter", "not_a_chart"],
                    }
                )
            for s in chart.get("bars", []):
                s["label"] = None
                s["axis"] = y_side
                s["resolved_by"] = "geometry"
                s["multivalued"] = False
                s["downsampled"] = False
                s["n_extrema_dropped"] = 0
        elif ctype == "scatter":
            chart["points"] = extract_scatter(
                small_paths, xa, ya, min_pts=3 if type_hinted else 5
            )
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
        _xlog = xa["scale"] == "log"
        _ylog = ya["scale"] == "log"
        _ylog_r = (panel["ya_right"] or ya)["scale"] == "log"
        dropped = 0
        if "curves" in chart:
            kept = []
            for c in chart["curves"]:
                right = c.get("axis") == "right"
                cyr = yr_right if right else yr
                if c.get("points") and not in_range_series(
                    c["points"], xr, cyr, xlog=_xlog, ylog=_ylog_r if right else _ylog
                ):
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
                    if pts and not in_range_series(pts, xr, yr, xlog=_xlog, ylog=_ylog):
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
        # an EXPLICITLY hinted type that still yields nothing must not return
        # a typed-but-empty chart (an ok with no data reads as "chart has no
        # series"); decline with the honest reason instead.
        if (
            type_hinted
            and chart["chart_type"] not in ("declined", "unknown")
            and not (chart.get("curves") or chart.get("bars") or chart.get("points"))
        ):
            chart["chart_type"] = "declined"
            chart["decline_reason"] = (
                f"hinted type '{ctype}' produced no extractable series — "
                "geometry too sparse or not vector-drawn"
            )
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
