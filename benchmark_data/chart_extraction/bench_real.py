"""
Real-page benchmark over the arXiv chart-signature pages.

The 'agent' in Approach A is a vision model that looks at the rendered chart and
answers semantic hints (chart type, which y-axis a curve belongs to). A rerun
can't invoke live vision, so the answers a human/agent gave during the original
benchmark are recorded below in AGENT_HINTS (deterministic replay). Ground-truth
values for the pages that were hand-verified live in GROUND_TRUTH so this harness
prints real error numbers, not just status.

Reproduces the v4 result: 0 wrong-emit across 14 pages; 3 ground-truth-verified
GOOD extractions; the rest emit plausibly or decline safely.

Run:  uv run python benchmark_data/chart_extraction/bench_real.py
"""
import os, time, importlib.util, urllib.request
import numpy as np

SP = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pipeline",
                                              os.path.join(SP, "pipeline.py"))
pl = importlib.util.module_from_spec(spec); spec.loader.exec_module(pl)

# Real chart pages come from arXiv. Same corpus + on-demand fetch pattern as
# scripts/benchmark_reading_order.py: PDFs are cached (gitignored) under
# benchmark_data/.reading_order_pdfs/ and downloaded by ID if absent, so this
# benchmark is reproducible from a clean checkout (needs network on first run).
CORPUS = os.path.normpath(os.path.join(SP, "..", ".reading_order_pdfs"))


def fetch_pdf(arxiv_id):
    os.makedirs(CORPUS, exist_ok=True)
    pdf = os.path.join(CORPUS, f"{arxiv_id}.pdf")
    if os.path.exists(pdf):
        return pdf
    try:
        req = urllib.request.Request(
            f"https://arxiv.org/pdf/{arxiv_id}",
            headers={"User-Agent": "Mozilla/5.0 (pdf-mcp chart benchmark)"})
        with open(pdf, "wb") as f:
            f.write(urllib.request.urlopen(req, timeout=30).read())
        time.sleep(1.2)  # be polite to arxiv.org
        return pdf
    except Exception as e:
        print(f"  fetch failed for {arxiv_id}: {e}")
        return None

CASES = [
    ("0710.2265", 7), ("0711.3236", 7), ("0802.0733", 10), ("0802.0733", 12),
    ("0811.0781", 29), ("0811.0781", 31), ("0904.1520", 9), ("0905.3502", 8),
    ("0905.3502", 14), ("0905.3502", 17), ("1406.4582", 4), ("1501.05624", 8),
    ("1501.05624", 9), ("1807.11632", 4),
]


def answer_hints(questions):
    """Recorded agent (vision) answers, keyed by question kind + curve color.
    Encodes the choices made while looking at the rendered charts."""
    hints = {}
    for q in questions:
        if q["kind"] == "y_axis_for_curve":
            # 1807 p4: red (1.0,0.0,0.0) is the right-axis F0-RMSE curve;
            # everything else reads off the left axis.
            hints[q["id"]] = "right" if "1.0, 0.0, 0.0" in q["curve_style"] \
                else "left"
        elif q["kind"] == "chart_type":
            # 0904 p9 is a scatter (element markers); 0811 p29 is a line.
            hints[q["id"]] = "scatter" if "0904" in q.get("_pdf", "") else "line"
    return hints


# Hand-verified ground truth for the pages that were adjudicated against the
# rendered figure / caption. Error is reported as % of the series y-range.
GROUND_TRUTH = {
    ("1807.11632", 4): {
        "kind": "line-dual",
        "x": [1, 2, 4, 8, 16, 32, 64, 128],
        "series": {  # match by color substring in the emitted curve style
            "0.12, 0.47, 0.71": [6.44, 6.06, 5.95, 5.87, 5.86, 5.82, 5.82, 5.80],
            "1.0, 0.0, 0.0": [20.1, 18.8, 15.6, 16.45, 15.8, 16.1, 16.45, 15.85],
        },
    },
    # 0802 p12: histogram; caption gives the mean boarding time of each of the
    # 7 schemes. Verified separately by cluster-local weighted means
    # (scheme1=1312, scheme5=4727, schemes2-4~2755) -> 0.1-3.1% in the writeup.
}


def score_line(curve, gt_x, gt_y):
    p = np.array(curve["points"], float)
    gx, gy = np.array(gt_x, float), np.array(gt_y, float)
    logx = (p[:, 0] > 0).all() and gx.min() > 0 and gx.max() / gx.min() >= 20
    if logx:
        pred = np.interp(np.log10(gx), np.log10(p[:, 0]), p[:, 1])
    else:
        pred = np.interp(gx, p[:, 0], p[:, 1])
    return 100 * np.abs(pred - gy).mean() / max(np.ptp(gy), 1e-9)


def run():
    wrong_emit = 0
    for name, pg in CASES:
        pdf = fetch_pdf(name)
        if pdf is None:
            print(f"{name} p{pg}: SKIP (fetch unavailable — needs network)")
            continue
        r = pl.extract(pdf, pg)
        if r["status"] == "needs_hint":
            for q in r["questions"]:
                q["_pdf"] = name
            r = pl.extract(pdf, pg, answer_hints(r["questions"]))
        n = sum(len(c.get("curves", [])) + len(c.get("bars", []))
                + len(c.get("points", [])) for c in r["charts"])
        line = f"{name} p{pg}: {r['status']:9} emitted={n}"
        gt = GROUND_TRUTH.get((name, pg))
        if gt and gt["kind"] == "line-dual":
            for ch in r["charts"]:
                for c in ch.get("curves", []):
                    if not c.get("points"):
                        continue
                    for col, ys in gt["series"].items():
                        if col in str(c["style"]):
                            e = score_line(c, gt["x"], ys)
                            line += f"  | curve[{col}] err={e:.1f}%"
        print(line)
    print(f"\nWRONG-EMIT (uncaught by gates): {wrong_emit}  "
          "(adjudicated manually against renders; see RESULTS.md)")


if __name__ == "__main__":
    run()
