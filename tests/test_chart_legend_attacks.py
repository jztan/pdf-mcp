"""Adversarial legend-masking fixtures (v8 review attack set).

Every PDF here is a vanilla-matplotlib legend/annotation layout that at some
point defeated the legend-signature masks and produced a trust-contract
violation: the legend FRAME emitted as the only curve (single-entry framed
legend), legend markers injected fabricated points into real scatter series
(ncol / unframed single-entry), a shaded-region annotation strip clipped a
curve crest, and border-banding ate a curve's own apex. The invariants below
pin all of them. Regenerate PDFs with legend_attacks/gen_attacks.py.
"""

from pathlib import Path

import pymupdf
import pytest

from pdf_mcp import chart_extractor

ATK = (
    Path(__file__).parent.parent
    / "benchmark_data"
    / "chart_extraction"
    / "legend_attacks"
)

# (fixture, expected scatter point-counts per series) — None = not a scatter
SCATTER_CASES = [
    ("atk_single_framed", [12]),
    ("atk_single_entry_scatter", [12]),
    ("atk_ncol2_scatter", [12, 12]),
    ("atk_ncol2_framed", [12, 12]),
    ("atk_sizes_scatter", [12, 12]),
]


@pytest.mark.parametrize("name,expected_ns", SCATTER_CASES)
def test_no_fabricated_legend_points(name, expected_ns):
    """Legend markers/frames must never add points or curves to the data."""
    doc = pymupdf.open(ATK / f"{name}.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    ch = r["charts"][0]
    assert ch["chart_type"] == "scatter", ch["chart_type"]
    assert not ch.get("curves"), "no fabricated curve (legend frame) allowed"
    ns = sorted(len(s["points"]) for s in ch.get("points", []))
    assert ns == sorted(expected_ns), ns


def test_shaded_region_annotations_do_not_clip_crest():
    """Annotations inside a shaded (fill-only) region are NOT a legend; their
    strips must not mask the curve underneath (GT crest y=4.0)."""
    doc = pymupdf.open(ATK / "atk_crest_emit.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    ch = r["charts"][0]
    assert ch["chart_type"] == "line"
    ys = [p[1] for p in ch["curves"][0]["points"]]
    assert max(ys) > 3.95, f"crest clipped: ymax={max(ys)}"


@pytest.mark.parametrize("name", ["atk_peak_emit", "atk_peak_in_strip"])
def test_border_bands_do_not_eat_data_curves(name):
    """Border-banding applies only to perimeter-hugging (frame-like) paths —
    never to a data curve's own bbox (that ate the apex and killed panels)."""
    doc = pymupdf.open(ATK / f"{name}.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    ch = r["charts"][0]
    assert ch["chart_type"] == "line" and ch.get("curves"), ch["chart_type"]
