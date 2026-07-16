"""
Benchmark the prototype against the synthetic ground-truth corpus.
Metrics per case: emitted?, matched-to-GT error (% of y-range), verdict.
Global: accuracy on emitted, WRONG-EMIT rate (>5% err), decline correctness.
Simulated agent: answers dual-axis / chart-type hints CORRECTLY (oracle-agent
upper bound; real-agent noise studied separately in bench_real.py).

Run:  uv run python benchmark_data/chart_extraction/bench_synthetic.py
"""

import json, os, sys
import numpy as np
import fitz

SP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SP, "..", "..", "src"))
from pdf_mcp import chart_extractor as v3

CORPUS = os.path.join(SP, "syn_corpus")
GT = json.load(open(os.path.join(CORPUS, "ground_truth.json")))


def series_error(gt_xy, pred_pts, kind):
    gx, gy = np.array(gt_xy[0], float), np.array(gt_xy[1], float)
    p = np.array(pred_pts, float)
    if kind in ("bar", "scatter"):
        # match each GT point to nearest predicted x; compare y
        errs = []
        for xg, yg in zip(gx, gy):
            i = np.argmin(np.abs(p[:, 0] - xg))
            errs.append(abs(p[i, 1] - yg))
        errs = np.array(errs)
    else:
        order = np.argsort(p[:, 0])
        px, py = p[order, 0], p[order, 1]
        lo, hi = px.min(), px.max()
        m = (gx >= lo) & (gx <= hi)
        if m.sum() < 3:
            return None
        errs = np.abs(np.interp(gx[m], px, py) - gy[m])
    rng = max(np.ptp(gy), 1e-9)
    return 100 * errs.mean() / rng


def best_match(gt, chart):
    """greedy: for each GT series find best-scoring emitted series"""
    preds = []
    for c in chart.get("curves", []):
        if c.get("points"):
            preds.append(("line", c["points"]))
    for b in chart.get("bars", []):
        preds.append(("bar", b["bars"]))
    for s in chart.get("points", []):
        preds.append(("scatter", s["points"]))
    return preds


def style_matches(c, series_style):
    style = c["style"]  # {"color": [r,g,b]|None, "width": float}
    return (
        style["color"] == series_style["color"]
        and style["width"] == series_style["width"]
    )


# max_points=12 pins the v7 sample density (extract_charts' production
# default is 24) so this benchmark reproduces the historical v7 numbers.
MAX_POINTS = 12

rows = []
wrong_emits = 0
total_emitted_series = 0
for name, gt in GT.items():
    pdf = os.path.join(CORPUS, f"{name}.pdf")
    doc = fitz.open(pdf)
    r = v3.extract_charts(doc, 0, max_points=MAX_POINTS)
    # oracle agent: answer dual-axis hints correctly by GT slope matching
    if r["status"] == "needs_hint":
        hints = {}
        for q in r["questions"]:
            if q["kind"] == "y_axis_for_curve":
                # oracle: try both; score ONLY the curve this question is about
                best = ("left", 1e9)
                for tag in ("left", "right"):
                    h = dict(hints)
                    h[q["id"]] = tag
                    rr = v3.extract_charts(doc, 0, h, max_points=MAX_POINTS)
                    for ch in rr["charts"]:
                        for c in ch.get("curves", []):
                            if not style_matches(c, q["series_style"]) or not c.get(
                                "points"
                            ):
                                continue
                            for sname, sxy in gt["series"].items():
                                e = series_error(sxy, c["points"], "line")
                                if e is not None and e < best[1]:
                                    best = (tag, e)
                hints[q["id"]] = best[0]
            elif q["kind"] == "chart_type":
                hints[q["id"]] = (
                    gt["type"]
                    if gt["type"] in ("line", "bar", "scatter")
                    else "not_a_chart"
                )
        r = v3.extract_charts(doc, 0, hints, max_points=MAX_POINTS)
        r["_hints_used"] = hints
    doc.close()

    emitted = []
    for ch in r["charts"]:
        emitted += best_match(gt, ch)
    if gt["type"] == "decline_expected":
        ok = not emitted
        rows.append(
            (
                name,
                r["status"],
                "correctly-declined" if ok else "WRONG-EMIT(decoy)",
                None,
            )
        )
        if not ok:
            wrong_emits += len(emitted)
            total_emitted_series += len(emitted)
        continue
    if not emitted:
        rows.append((name, r["status"], "declined(missed)", None))
        continue
    # score each GT series against best emitted candidate
    for sname, sxy in gt["series"].items():
        best_e = None
        for kind, pts in emitted:
            e = series_error(sxy, pts, kind)
            if e is not None and (best_e is None or e < best_e):
                best_e = e
        total_emitted_series += 1
        if best_e is None:
            rows.append((name, r["status"], f"{sname}: no-match", None))
            wrong_emits += 1
        else:
            tag = "OK" if best_e <= 5 else "WRONG-EMIT"
            if best_e > 5:
                wrong_emits += 1
            rows.append((name, r["status"], f"{sname}: {tag}", best_e))

print(f"{'case':26} {'status':11} {'verdict':28} err%range")
for n, s, v, e in rows:
    print(f"{n:26} {s:11} {v:28} {'' if e is None else f'{e:.2f}'}")
ok_errs = [e for _, _, v, e in rows if e is not None and "OK" in v]
print(
    f"\nEmitted-series accuracy: mean {np.mean(ok_errs):.2f}% of y-range "
    f"(n={len(ok_errs)})"
    if ok_errs
    else "no accurate emissions"
)
print(f"WRONG-EMIT count: {wrong_emits} / {total_emitted_series} emitted series")
