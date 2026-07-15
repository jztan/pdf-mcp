"""Synthetic benchmark corpus: 10 chart archetypes with exact ground truth.

Regenerates syn_corpus/*.pdf + ground_truth.json deterministically (fixed
seeds). Only step that needs matplotlib; running the benchmarks does not.

Run (needs matplotlib):
    python benchmark_data/chart_extraction/gen_synthetic.py
"""

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np, json, os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "syn_corpus")
os.makedirs(OUT, exist_ok=True)
np.random.seed(42)
GT = {}


def save(fig, name, gt):
    fig.savefig(os.path.join(OUT, f"{name}.pdf"))
    plt.close(fig)
    GT[name] = gt


# 1. clean color line, linear
x = np.linspace(0, 10, 11)
y = 2 * x + 5
fig, ax = plt.subplots(figsize=(5, 4))
ax.plot(x, y, color="tab:red")
ax.set_xticks(range(0, 11, 2))
ax.set_yticks(range(0, 31, 5))
save(
    fig,
    "line_color_linear",
    {"type": "line", "series": {"red": [x.tolist(), y.tolist()]}},
)

# 2. mono line + black grid
x = np.linspace(0, 10, 21)
y = 3 * np.sin(0.6 * x) + 0.4 * x + 10
fig, ax = plt.subplots(figsize=(5, 4))
ax.grid(True, color="black")
ax.plot(x, y, color="black")
ax.set_xticks(range(0, 11, 2))
ax.set_yticks(range(0, 21, 4))
save(
    fig,
    "line_mono_grid",
    {"type": "line", "series": {"black": [x.tolist(), y.tolist()]}},
)

# 3. log-x line
x = np.array([1, 2, 4, 8, 16, 32, 64, 128], float)
y = 6.5 - 0.5 * np.log10(x) * 2
fig, ax = plt.subplots(figsize=(5, 4))
ax.semilogx(x, y, color="tab:blue", marker="^")
save(fig, "line_logx", {"type": "line", "series": {"blue": [x.tolist(), y.tolist()]}})

# 4. dual-axis two lines
x = np.linspace(0, 10, 11)
y1 = 5 + 0.3 * x
y2 = 200 - 12 * x
fig, ax = plt.subplots(figsize=(5, 4))
ax.plot(x, y1, color="tab:blue")
ax.set_ylim(4, 9)
ax.set_ylabel("A")
ax2 = ax.twinx()
ax2.plot(x, y2, color="tab:red")
ax2.set_ylim(0, 220)
ax2.set_ylabel("B")
save(
    fig,
    "line_dual_axis",
    {
        "type": "line",
        "series": {
            "blue_left": [x.tolist(), y1.tolist()],
            "red_right": [x.tolist(), y2.tolist()],
        },
    },
)

# 5. two-series color + legend + dashed
x = np.linspace(0, 10, 11)
y1 = 1.5 * x + 3
y2 = 0.5 * x**1.4 + 2
fig, ax = plt.subplots(figsize=(5, 4))
ax.plot(x, y1, color="tab:green", label="alpha")
ax.plot(x, y2, color="tab:purple", linestyle="--", label="beta")
ax.legend()
save(
    fig,
    "line_two_legend_dashed",
    {
        "type": "line",
        "series": {
            "green": [x.tolist(), y1.tolist()],
            "purple_dashed": [x.tolist(), y2.tolist()],
        },
    },
)

# 6. bar chart
cats = np.arange(1, 8)
vals = np.array([12, 18, 7, 22, 15, 9, 25], float)
fig, ax = plt.subplots(figsize=(5, 4))
ax.bar(cats, vals, color="tab:blue")
ax.set_xticks(cats)
save(
    fig,
    "bar_simple",
    {"type": "bar", "series": {"blue": [cats.tolist(), vals.tolist()]}},
)

# 7. histogram (mono, filled) — the 0802 failure archetype
data = np.concatenate([np.random.normal(30, 4, 400), np.random.normal(60, 6, 300)])
fig, ax = plt.subplots(figsize=(5, 4))
counts, edges, _ = ax.hist(data, bins=25, color="black")
centers = (edges[:-1] + edges[1:]) / 2
save(
    fig,
    "hist_mono",
    {"type": "bar", "series": {"black": [centers.tolist(), counts.tolist()]}},
)

# 8. scatter
xs = np.random.uniform(0, 10, 30)
ys2 = 2 * xs + np.random.normal(0, 2, 30) + 3
fig, ax = plt.subplots(figsize=(5, 4))
ax.scatter(xs, ys2, color="tab:orange", s=18)
save(
    fig,
    "scatter_simple",
    {"type": "scatter", "series": {"orange": [xs.tolist(), ys2.tolist()]}},
)

# 9. crossing mono multi-line (should DECLINE curves)
x = np.linspace(0, 10, 40)
fig, ax = plt.subplots(figsize=(5, 4))
for k in range(6):
    ax.plot(x, np.sin(x + k) + 0.1 * k * x, color="black", linewidth=0.8)
save(fig, "line_mono_crossing", {"type": "decline_expected", "series": {}})

# 10. not-a-chart decoy: labeled diagram w/ numbers (should DECLINE at signature)
fig, ax = plt.subplots(figsize=(5, 4))
ax.axis("off")
for i in range(1, 6):
    c = plt.Circle((i * 1.5, 2.0), 0.4, fill=False)
    ax.add_patch(c)
    ax.text(i * 1.5, 2.0, str(i), ha="center", va="center")
    ax.plot([i * 1.5 + 0.4, i * 1.5 + 1.1], [2.0, 2.0], color="black")
ax.set_xlim(0, 10)
ax.set_ylim(0, 4)
save(fig, "decoy_diagram", {"type": "decline_expected", "series": {}})

# 11. German-locale axis: thousands written with PERIOD ("10.000" = 10000).
# Token-level parsing is genuinely ambiguous vs EN decimals -> the pipeline
# must DECLINE, never emit a 1000x mis-scaled table (verified wrong-emit
# before the locale gate; LLM-consumer review 2026-07-13).
x = np.array([0, 5000, 10000, 15000, 20000], float)
y = np.array([2, 9, 14, 17, 19], float)
fig, ax = plt.subplots(figsize=(5, 4))
ax.plot(x, y, color="tab:red")
ax.set_xticks(x)
ax.set_xticklabels(["0", "5.000", "10.000", "15.000", "20.000"])
ax.set_yticks(range(0, 21, 5))
save(fig, "line_locale_de", {"type": "decline_expected", "series": {}})

# 12. negative log exponents — matplotlib renders the minus as U+2212;
# without normalization every negative tick drops and the chart declines.
x = np.logspace(-3, 0, 10)
y = 9 + 2 * np.log10(x)
fig, ax = plt.subplots(figsize=(5, 4))
ax.semilogx(x, y, color="tab:blue")
save(
    fig, "line_neg_log", {"type": "line", "series": {"blue": [x.tolist(), y.tolist()]}}
)

# 13. sharp peak between uniform sample slots — extrema-preserving sampling
# must retain it ("point-exact, feature-lossy" trap; consumer review).
x = np.linspace(0, 10, 201)
y = 10 + 5 * np.sin(x)
y[y.shape[0] // 2 + 1] = 100.0  # spike at x=5.05
fig, ax = plt.subplots(figsize=(5, 4))
ax.plot(x, y, color="tab:red")
ax.set_yticks(range(0, 101, 20))
save(
    fig,
    "line_sharp_peak",
    {"type": "line", "series": {"red": [x.tolist(), y.tolist()]}},
)

# 14. line chart + colorbar (the arXiv 2001.08361 p24 Fig18 wrong-emit
# archetype): a compact 3-tick panel y-axis (100/300/500, pixel span <60,
# same shape as the real Fig18 subplot) sits next to a taller ScalarMappable
# colorbar (0..10, "Test Loss") whose own tick column spans MORE pixels —
# under the pre-fix 60pt threshold the real axis was rejected and the
# colorbar's ticks won as the right-side axis by default. Ground truth is
# the two REAL line series on the panel's own y-axis (100..500) — the data
# range is far from the colorbar's 0..10 so a chimera (colorbar-calibrated y,
# order-of-magnitude smaller) is detectable by range alone.
x = np.linspace(0, 10, 11)
y1 = 150 + 30 * x
y2 = 480 - 25 * x
fig, ax = plt.subplots(figsize=(5, 4))
ax.plot(x, y1, color="tab:blue")
ax.plot(x, y2, color="tab:red")
ax.set_ylim(100, 500)
ax.set_yticks([100, 300, 500])
sm = plt.cm.ScalarMappable(cmap="viridis", norm=plt.Normalize(0, 10))
sm.set_array([])
cbar = fig.colorbar(sm, ax=ax, pad=0.0)
cbar.set_label("Test Loss")
# shrink ONLY the plot axes (not the colorbar) so the panel's own y-axis
# tick-label pixel span is compact (~46pt) while the colorbar's tick column
# stays tall — reproducing the real-world geometry that fooled the pre-fix
# 60pt span filter.
pos = ax.get_position()
ax.set_position([pos.x0, pos.y0, pos.width, 0.16])
save(
    fig,
    "line_colorbar",
    {
        "type": "line",
        "series": {"blue": [x.tolist(), y1.tolist()], "red": [x.tolist(), y2.tolist()]},
    },
)

# 14. power-of-two LOG axis (base-2 superscript ticks 2^19..2^27) — read as
# plain integers "219..227" this fits a LINEAR axis at r2=1.0 and silently
# emits values off by orders of magnitude (verified wrong-emit on Hestness
# 1712.00409). The base^exp superscript reader must recover base 2.
xk = np.arange(19, 28)
x = 2.0**xk
y = 12.0 - 0.5 * xk
fig, ax = plt.subplots(figsize=(5, 4))
ax.plot(x, y, color="tab:blue")
ax.set_xscale("log", base=2)
ax.set_yticks(range(0, 13, 3))
save(fig, "line_log2", {"type": "line", "series": {"blue": [x.tolist(), y.tolist()]}})

# 15. NEGATIVE-decade log y-axis (ticks 10^-4..10^0) — the SGDR/Henighan
# wrong-emit class: matplotlib mathtext kerns the raised exponent to overlap
# the base (pairing must accept ~-2pt gaps) and, in some backends, draws the
# exponent's minus as an hrule instead of a glyph. The trust-contract
# invariant for this archetype is NO WRONG EMIT: either the axis reads as
# log [1e-4, 1] (typed minus) or the chart declines (drawn minus) — never a
# linear [-4, 0] axis.
x = np.linspace(0, 200, 9)
y = 10.0 ** (-4 + 4 * np.exp(-x / 60.0))
fig, ax = plt.subplots(figsize=(5, 4))
ax.plot(x, y, color="tab:green")
ax.set_yscale("log")
ax.set_ylim(5e-5, 2.0)
ax.set_xticks(range(0, 201, 50))
save(
    fig, "line_logneg", {"type": "line", "series": {"green": [x.tolist(), y.tolist()]}}
)

# 16. SPARSE marker line (5 points, one per "model size") — the canonical
# scaling-law figure. Below the dense-cloud gate (>=8 vertices), it must be
# recovered via marker-vertex coincidence, not fall through to "unknown".
x = np.array([1.0, 2.0, 4.0, 8.0, 16.0])
y = np.array([61.0, 67.0, 72.0, 74.5, 76.0])
fig, ax = plt.subplots(figsize=(5, 4))
ax.plot(x, y, color="tab:purple", marker="o")
ax.set_xticks([0, 4, 8, 12, 16])
ax.set_yticks(range(60, 81, 5))
save(
    fig, "line_sparse", {"type": "line", "series": {"purple": [x.tolist(), y.tolist()]}}
)

# --- hint-flow adversarial fixtures (NO ground-truth entries: they pin the
# HINTED extraction paths in pytest, not the oracle-driven benchmark) -------
# 17. sparse marker-less line + significance bracket: answering the
# chart_type question with "line" must NOT emit the bracket as a curve
# (adversarial review probe; the marker-connection requirement is mandatory
# even under a hint).
x = np.array([1.0, 4.0, 7.0, 10.0])
y = np.array([5.0, 9.0, 12.0, 14.0])
fig, ax = plt.subplots(figsize=(5, 4))
ax.plot(x, y, color="tab:red")  # 4 vertices, no markers
bx1, bx2, by = 3.0, 7.5, 18.0
ax.plot([bx1, bx1, bx2, bx2], [by - 0.6, by, by, by - 0.6], color="black", lw=1)
ax.set_xlim(0, 11)
ax.set_ylim(0, 20)
ax.set_xticks(range(0, 12, 2))
ax.set_yticks(range(0, 21, 5))
fig.savefig(os.path.join(OUT, "line_bracket_decoy.pdf"))
plt.close(fig)

# 18. 4 real scatter markers + two same-color annotation arrowheads:
# answering "scatter" must emit ONLY the real 4-point series — the
# arrowhead pair must stay below the hinted min-points gate.
xs = np.array([2.0, 4.0, 6.0, 8.0])
ys = np.array([4.0, 8.0, 11.0, 15.0])
fig, ax = plt.subplots(figsize=(5, 4))
ax.scatter(xs, ys, color="tab:blue", s=25)
for ax_, ay_ in [(3.0, 17.0), (7.0, 17.0)]:
    ax.annotate(
        "",
        xy=(ax_, 15.5),
        xytext=(ax_, 17.8),
        arrowprops=dict(arrowstyle="-|>", color="tab:blue", mutation_scale=18),
    )
ax.set_xlim(0, 10)
ax.set_ylim(0, 20)
ax.set_xticks(range(0, 11, 2))
ax.set_yticks(range(0, 21, 5))
fig.savefig(os.path.join(OUT, "scatter_arrow_decoy.pdf"))
plt.close(fig)

with open(os.path.join(OUT, "ground_truth.json"), "w") as f:
    json.dump(GT, f, indent=1)
print(f"wrote {len(GT)} synthetic PDFs + ground_truth.json to {OUT}")
