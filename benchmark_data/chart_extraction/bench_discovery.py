"""
Discovery benchmark: can the chart-signature check tell agents WHICH pages
have extractable charts? (The `detect_charts` signal in the design — the
consumer-side risk is an agent that never calls the extractor.)

Signal under test: find_panels(page) returns >= 1 panel (anchored tick-series
signature). Swept over every page of the reading-order corpus + the issue-23
samples. Scored against pages adjudicated during the v4-v6 benchmark work.

Metrics: recall on known chart pages, false positives on known non-chart
pages (diagrams/boards/heatmaps that fooled naive censuses), flag rate over
the whole corpus, and per-page runtime (the signal rides on pdf_read_pages).

Run:  uv run python benchmark_data/chart_extraction/bench_discovery.py
"""

import glob, os, sys, time
import fitz

SP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SP, "..", "..", "src"))
from pdf_mcp import chart_extractor as pl

CORPUS = os.path.normpath(os.path.join(SP, "..", ".reading_order_pdfs"))
LOCAL = os.path.normpath(os.path.join(SP, "..", ".chart_samples"))

# Visually adjudicated during the v4-v6 sessions (renders inspected).
KNOWN_CHART_PAGES = {
    ("0710.2265", 7),  # pacing response diagram (line)
    ("0711.3236", 7),  # step functions b(x)/s(x)
    ("0802.0733", 10),  # overlaid histograms
    ("0802.0733", 12),  # boarding-time histogram (caption GT)
    ("0811.0781", 29),  # line chart (w/ inset)
    ("0811.0781", 31),  # 3-panel line figure
    ("0904.1520", 9),  # scatter (element markers)
    ("1406.4582", 4),  # CI panels + persistence scatter figure
    ("1501.05624", 8),  # multi-line drift chart (+ heatmaps)
    ("1501.05624", 9),  # line panels
    ("1807.11632", 4),  # dual-axis line chart
    ("2605.06546", 20),  # Fig10 log-log + Fig11 small multiples
    ("2203.15556", 5),  # Chinchilla Fig2 (envelope + 2 scatters)
    ("littelfuse_sp05", 2),  # capacitance vs reverse voltage
    ("0709.4466", 3),  # sensitivity histogram (initially mislabeled
    # non-chart by paper-level inference; the discovery
    # flag was right — adjudicated 2026-07-13)
    ("1207.2761", 4),  # GPS distance-detection line chart (adjudicated)
    ("1612.09007", 4),  # loss-convergence curve + confusion matrix (adjud.)
}
KNOWN_NON_CHART_PAGES = {
    ("0811.0851", 7),  # peg-solitaire boards (numbers, no chart)
    ("0706.2397", 17),  # topology illustration (chromatic, no axes)
    ("0707.3690", 4),  # geometric lattice diagram
    ("0710.2265", 5),  # raster heatmap panels (colorbar ticks)
}


def discover(page):
    return len(pl.find_panels(page)) > 0


def run():
    flags, times = {}, []
    pdfs = sorted(glob.glob(os.path.join(CORPUS, "*.pdf")))
    lf = os.path.join(LOCAL, "littelfuse_sp05.pdf")
    if os.path.exists(lf):
        pdfs.append(lf)
    n_pages = 0
    for p in pdfs:
        name = os.path.basename(p).replace(".pdf", "")
        doc = fitz.open(p)
        for i, page in enumerate(doc):
            t0 = time.perf_counter()
            try:
                hit = discover(page)
            except Exception:
                hit = False
            times.append(time.perf_counter() - t0)
            n_pages += 1
            if hit:
                flags[(name, i + 1)] = True
        doc.close()

    tp = [k for k in KNOWN_CHART_PAGES if flags.get(k)]
    fn = [k for k in KNOWN_CHART_PAGES if not flags.get(k)]
    fp_known = [k for k in KNOWN_NON_CHART_PAGES if flags.get(k)]
    unlabeled = [
        k
        for k in flags
        if k not in KNOWN_CHART_PAGES and k not in KNOWN_NON_CHART_PAGES
    ]

    times_ms = sorted(t * 1000 for t in times)
    print(
        f"pages swept: {n_pages}   flagged: {len(flags)} "
        f"({100*len(flags)/n_pages:.1f}%)"
    )
    print(
        f"runtime/page: median {times_ms[len(times_ms)//2]:.1f}ms  "
        f"p95 {times_ms[int(len(times_ms)*0.95)]:.1f}ms  "
        f"max {times_ms[-1]:.0f}ms"
    )
    print(f"\nrecall on known chart pages: {len(tp)}/{len(KNOWN_CHART_PAGES)}")
    for k in sorted(fn):
        print(f"   MISSED: {k[0]} p{k[1]}")
    print(
        f"false positives on known non-chart pages: "
        f"{len(fp_known)}/{len(KNOWN_NON_CHART_PAGES)}"
    )
    for k in sorted(fp_known):
        print(f"   FALSE FLAG: {k[0]} p{k[1]}")
    print(f"\nflagged but unlabeled ({len(unlabeled)}) — adjudicate visually:")
    for k in sorted(unlabeled):
        print(f"   {k[0]} p{k[1]}")


if __name__ == "__main__":
    run()
