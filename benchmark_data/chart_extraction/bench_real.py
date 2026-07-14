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

import os, time, sys, urllib.request
import numpy as np
import fitz

SP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SP, "..", "..", "src"))
from pdf_mcp import chart_extractor as pl

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
            headers={"User-Agent": "Mozilla/5.0 (pdf-mcp chart benchmark)"},
        )
        with open(pdf, "wb") as f:
            f.write(urllib.request.urlopen(req, timeout=30).read())
        time.sleep(1.2)  # be polite to arxiv.org
        return pdf
    except Exception as e:
        print(f"  fetch failed for {arxiv_id}: {e}")
        return None


CASES = [
    ("0710.2265", 7),
    ("0711.3236", 7),
    ("0802.0733", 10),
    ("0802.0733", 12),
    ("0811.0781", 29),
    ("0811.0781", 31),
    ("0904.1520", 9),
    ("0905.3502", 8),
    ("0905.3502", 14),
    ("0905.3502", 17),
    ("1406.4582", 4),
    ("1501.05624", 8),
    ("1501.05624", 9),
    ("1807.11632", 4),
    # issue-#23 reporter's own samples (arXiv, auto-fetched):
    ("2605.06546", 20),  # Fig 11: 6 small-multiple panels x 6 series (+ Fig
    # 10 declines: y-axis has 2 composite N x 10^k labels)
    ("2203.15556", 5),  # Chinchilla IsoFLOP: crossing envelope declines;
    # 2 tractable panels emit (adjudicated v6)
]

# issue-#23 samples that cannot be auto-fetched (bot-walled / proprietary —
# not redistributed). Download manually into benchmark_data/.chart_samples/
# (gitignored); cases SKIP when absent.
#   littelfuse_sp05.pdf: https://www.littelfuse.com/assetdocs/
#     tvs-diode-array-spasp050xba-lead-freegreen-datasheet
#     ?assetguid=15a03de1-f0c6-457a-95f1-55d449fdd756
LOCAL_DIR = os.path.normpath(os.path.join(SP, "..", ".chart_samples"))
LOCAL_CASES = [
    ("littelfuse_sp05", 2),  # "Typical Diode Capacitance vs Reverse Voltage"
]


def answer_hints(questions):
    """Recorded agent (vision) answers, keyed by question kind + curve color.
    Encodes the choices made while looking at the rendered charts."""
    hints = {}
    for q in questions:
        if q["kind"] == "y_axis_for_curve":
            # 1807 p4: red (1.0,0.0,0.0) is the right-axis F0-RMSE curve;
            # everything else reads off the left axis.
            hints[q["id"]] = (
                "right" if "1.0, 0.0, 0.0" in str(q["series_style"]) else "left"
            )
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
    ("littelfuse_sp05", 2): {
        "kind": "line-dual",  # single curve; same color-matched scoring path
        "x": [0, 1, 2, 3, 4, 5],
        # visual reads off the rendered figure (+/- ~0.5 pF)
        "series": {"0.0, 0.53, 0.32": [50.0, 38.8, 33.0, 29.8, 27.7, 26.0]},
    },
    ("2605.06546", 20): {
        "kind": "line-dual",
        "x": [1, 2, 3, 5],
        # Fig 11 top-left panel, darkest-blue series (r=0.1), read off a 6x
        # zoomed render. The same style recurs in all 6 panels; scoring takes
        # the best-matching curve (i.e. the panel this GT belongs to).
        "series": {"0.23, 0.32, 0.54": [0.3633, 0.3688, 0.3766, 0.3832]},
    },
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


def run_case(name, pg, pdf):
    # max_points=12 pins the v7 sample density (extract_charts' production
    # default is 24) so this benchmark reproduces the historical v7 numbers.
    doc = fitz.open(pdf)
    r = pl.extract_charts(doc, pg - 1, max_points=12)
    # hint burden: how many questions text self-answering (tier 2) left open
    # for the vision agent, before any hint is applied
    n_q = len(r.get("questions", []))
    if r["status"] == "needs_hint":
        for q in r["questions"]:
            q["_pdf"] = name
        r = pl.extract_charts(doc, pg - 1, answer_hints(r["questions"]), max_points=12)
    doc.close()
    n = sum(
        len(c.get("curves", [])) + len(c.get("bars", [])) + len(c.get("points", []))
        for c in r["charts"]
    )
    line = f"{name} p{pg}: {r['status']:9} emitted={n} questions={n_q}"
    gt = GROUND_TRUTH.get((name, pg))
    if gt and gt["kind"] == "line-dual":
        for col, ys in gt["series"].items():
            errs = []
            for ch in r["charts"]:
                for c in ch.get("curves", []):
                    if c.get("points") and col in str(c["style"]):
                        e = score_line(c, gt["x"], ys)
                        if e is not None:
                            errs.append(e)
            if errs:
                # style may recur across panels (small multiples): the GT
                # belongs to one panel, so score the best-matching curve
                line += f"  | curve[{col}] err={min(errs):.1f}%"
            else:
                line += f"  | curve[{col}] NOT EMITTED"
    print(line)


def run():
    for name, pg in CASES:
        pdf = fetch_pdf(name)
        if pdf is None:
            print(f"{name} p{pg}: SKIP (fetch unavailable — needs network)")
            continue
        run_case(name, pg, pdf)
    for name, pg in LOCAL_CASES:
        pdf = os.path.join(LOCAL_DIR, f"{name}.pdf")
        if not os.path.exists(pdf):
            print(
                f"{name} p{pg}: SKIP (download manually into "
                f"{LOCAL_DIR}/ — see comment above LOCAL_CASES)"
            )
            continue
        run_case(name, pg, pdf)
    print(
        "\nWRONG-EMIT (uncaught by gates): 0 as of v5 — "
        "adjudicated manually against renders; see RESULTS.md"
    )


if __name__ == "__main__":
    run()
