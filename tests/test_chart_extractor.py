"""Unit tests for chart_extractor — the pure-logic chart extraction module."""

from pathlib import Path

import pymupdf
import pytest

from pdf_mcp import chart_extractor

SYN = (
    Path(__file__).parent.parent / "benchmark_data" / "chart_extraction" / "syn_corpus"
)


@pytest.fixture(scope="module")
def line_doc():
    doc = pymupdf.open(SYN / "line_color_linear.pdf")
    yield doc
    doc.close()


@pytest.fixture(scope="module")
def dual_doc():
    doc = pymupdf.open(SYN / "line_dual_axis.pdf")
    yield doc
    doc.close()


def test_extract_charts_takes_open_doc_zero_indexed(line_doc):
    result = chart_extractor.extract_charts(line_doc, 0)
    assert result["status"] == "ok"
    assert result["charts"][0]["chart_id"] == "p0"
    curve = result["charts"][0]["curves"][0]
    assert curve["points"], "line curve must emit points"


def test_question_ids_are_positional(dual_doc):
    result = chart_extractor.extract_charts(dual_doc, 0)
    assert result["status"] == "needs_hint"
    ids = [q["id"] for q in result["questions"]]
    # positional: p{panel}.s{series}.axis — never style-derived
    assert all(i.startswith("p0.s") and i.endswith(".axis") for i in ids)
    # style still present for display
    assert all("series_style" in q for q in result["questions"])


def test_needs_hint_curves_carry_no_points(dual_doc):
    """Trust-contract regression: a curve whose axis question is still OPEN
    must never carry a numeric points table. Pre-fix, extract_charts left
    the curve's geometry-extracted "points" (calibrated against the default
    left axis) on the curve alongside resolved_by="geometry" — a wrong-table
    escape path, since the real axis is unknown until the caller answers.
    """
    result = chart_extractor.extract_charts(dual_doc, 0)
    assert result["status"] == "needs_hint"
    pending_ids = {q["id"] for q in result["questions"]}
    assert pending_ids
    for ch in result["charts"]:
        for ci, c in enumerate(ch.get("curves", [])):
            akey = f"{ch['chart_id']}.s{ci}.axis"
            if akey in pending_ids:
                assert "points" not in c, f"{akey} must not carry a points table"
                assert c.get("axis") is None
                assert c.get("pending_question") == akey
                # style/label survive so callers can still correlate the
                # question to a visual series
                assert "style" in c
                assert "label" in c


def test_hints_resolve_dual_axis(dual_doc):
    r1 = chart_extractor.extract_charts(dual_doc, 0)
    hints = {q["id"]: "left" for q in r1["questions"]}
    # blue is left in this fixture; red right — answer red correctly.
    # (fixture uses matplotlib "tab:red" = rgb(0.84, 0.15, 0.16), not pure
    # red, so the red-channel threshold is 0.5, not 0.9 — tab:blue's red
    # channel is 0.12, so 0.5 cleanly discriminates the two series.)
    for q in r1["questions"]:
        if q["series_style"]["color"] and q["series_style"]["color"][0] > 0.5:
            hints[q["id"]] = "right"
    r2 = chart_extractor.extract_charts(dual_doc, 0, hints=hints)
    assert r2["status"] == "ok"
    axes = {c.get("axis") for ch in r2["charts"] for c in ch["curves"]}
    assert axes == {"left", "right"}


def test_declined_chart_carries_reason_in_diagnostics_notes():
    doc = pymupdf.open(SYN / "line_mono_crossing.pdf")
    try:
        result = chart_extractor.extract_charts(doc, 0)
    finally:
        doc.close()
    assert result["status"] == "declined"
    declined = [c for c in result["charts"] if c["chart_type"] == "declined"]
    assert declined, "expected at least one declined chart"
    chart = declined[0]
    assert chart["decline_reason"]
    notes = chart["diagnostics"]["notes"]
    assert any("multivalued" in n for n in notes)


def test_annotated_hint_render(tmp_path, dual_doc):
    result = chart_extractor.extract_charts(dual_doc, 0)
    assert result["status"] == "needs_hint"
    chart_extractor.annotate_questions(dual_doc, 0, result, tmp_path, "testhash")
    q = result["questions"][0]
    assert Path(q["render_path"]).exists()
    assert q["highlight"] in chart_extractor._HALO_NAMES
    # halo hue must genuinely contrast with the series' own color — not just
    # differ in name — so channel-wise distance must clear a real threshold.
    halo_rgb = chart_extractor._HALOS[q["highlight"]]
    series_rgb = q["series_style"]["color"] or (0, 0, 0)
    dist = sum(abs(a - b) for a, b in zip(halo_rgb, series_rgb))
    assert dist >= 0.8


def _pixels(pix):
    import numpy as np

    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    return arr.reshape(pix.height, pix.width, pix.n)[..., :3].astype(int)


def test_annotated_hint_render_pixels_show_halo(tmp_path, dual_doc):
    """The halos must be VISIBLE in the saved PNG, not silently buried
    (regression: overlay=False hid them under the opaque plot background).
    Compares annotated vs unannotated renders of the same clip pixel-wise."""
    import numpy as np

    result = chart_extractor.extract_charts(dual_doc, 0)
    chart_extractor.annotate_questions(dual_doc, 0, result, tmp_path, "pxhash")
    annotated = _pixels(pymupdf.Pixmap(result["questions"][0]["render_path"]))
    # baseline: identical clip + dpi rendered straight from the source page
    page = dual_doc[0]
    panel = chart_extractor.find_panels(page)[0]
    clip = pymupdf.Rect(
        panel["rx0"] - 5, panel["ry0"] - 5, panel["rx1"] + 5, panel["ry1"] + 5
    )
    base = _pixels(page.get_pixmap(dpi=200, clip=clip))
    assert annotated.shape == base.shape
    changed = np.abs(annotated - base).sum(axis=2) > 30
    # a real halo band repaints a substantial share of the panel
    assert changed.mean() > 0.005, f"only {changed.mean():.4%} of pixels changed"
    # ...and among the changed pixels, each queried series must have at least
    # one pixel whose color is closer to its halo RGB than to white or to the
    # series' own color (pure background/stroke shifts would fail this)
    cpx = annotated[changed]
    for q in result["questions"]:
        halo = np.array(chart_extractor._HALOS[q["highlight"]]) * 255
        series = np.array(q["series_style"]["color"] or (0, 0, 0)) * 255
        d_halo = np.abs(cpx - halo).sum(axis=1)
        d_white = np.abs(cpx - 255).sum(axis=1)
        d_series = np.abs(cpx - series).sum(axis=1)
        assert (
            (d_halo < d_white) & (d_halo < d_series)
        ).any(), f"no halo-colored pixels for {q['id']} ({q['highlight']})"


def test_detect_charts_signal(line_doc):
    n = chart_extractor.detect_charts_signal(line_doc[0])
    assert n == 1


def test_detect_charts_signal_budget_returns_none(line_doc):
    assert chart_extractor.detect_charts_signal(line_doc[0], budget_ms=0) is None


def test_version_constant():
    assert isinstance(chart_extractor.CHART_EXTRACTION_VERSION, int)


def test_hints_hash_order_independent():
    a = chart_extractor.hints_hash({"p0.type": "bar", "p0.s1.axis": "right"})
    b = chart_extractor.hints_hash({"p0.s1.axis": "right", "p0.type": "bar"})
    assert a == b


def test_hints_hash_none_equals_empty_dict():
    assert chart_extractor.hints_hash(None) == chart_extractor.hints_hash({})


def test_sharp_peak_survives_sampling():
    doc = pymupdf.open(SYN / "line_sharp_peak.pdf")
    result = chart_extractor.extract_charts(doc, 0, max_points=24)
    doc.close()
    assert result["status"] == "ok"
    pts = result["charts"][0]["curves"][0]["points"]
    ys = [p[1] for p in pts]
    # ground truth peak is y=100 at x=5.03 (between uniform sample slots)
    assert max(ys) > 95, f"peak lost: max emitted y={max(ys)}"


def test_extrema_overflow_self_reports():
    doc = pymupdf.open(SYN / "line_sharp_peak.pdf")
    # budget of 6 points cannot hold the jagged section's extrema
    result = chart_extractor.extract_charts(doc, 0, max_points=6)
    doc.close()
    notes = result["charts"][0]["diagnostics"].get("notes", [])
    assert any("extrema exceeded max_points" in n for n in notes)


def test_global_max_survives_adversarial_prominence():
    import numpy as np

    from pdf_mcp.chart_extractor import _select_sample_indices

    # gentle global hill (apex 100) plus four sharper, higher-local-
    # prominence spikes (amplitude 40): local prominence ranking alone
    # would fill the budget with spikes and drop the true global max.
    xs = np.linspace(0.0, 10.0, 401)
    ys = 100.0 * np.exp(-((xs - 5.0) ** 2) / 8.0)
    for cx in (1.0, 2.0, 8.0, 9.0):
        ys = ys + 40.0 * (np.abs(xs - cx) < 0.03)
    sel = _select_sample_indices(ys, 6)
    assert int(np.argmax(ys)) in sel, "global max dropped by sampler"
    assert 0 in sel and len(ys) - 1 in sel


def test_max_points_floor():
    doc = pymupdf.open(SYN / "line_sharp_peak.pdf")
    result = chart_extractor.extract_charts(doc, 0, max_points=1)
    doc.close()
    pts = result["charts"][0]["curves"][0]["points"]
    assert len(pts) <= 4


REAL = Path(__file__).parent.parent / "benchmark_data" / ".reading_order_pdfs"
LOCAL = Path(__file__).parent.parent / "benchmark_data" / ".chart_samples"


@pytest.mark.skipif(
    not (REAL / "1807.11632.pdf").exists(), reason="real corpus not fetched"
)
def test_1807_dual_axis_resolves_via_text_no_hints():
    doc = pymupdf.open(REAL / "1807.11632.pdf")
    result = chart_extractor.extract_charts(doc, 3)  # page 4, 0-indexed
    doc.close()
    # legend "MCD"/"F0 RMSE" + right-axis title "F0 RMSE (Hz)" resolve the
    # axis assignment with ZERO questions
    assert result["status"] == "ok", result.get("questions")
    reds = [
        c
        for ch in result["charts"]
        for c in ch["curves"]
        if c["style"]["color"] and c["style"]["color"][0] == 1.0
    ]
    assert reds and reds[0]["axis"] == "right"
    assert reds[0]["resolved_by"] == "text"
    assert reds[0].get("label") == "F0 RMSE"


def test_style_collision_disables_text_answer(dual_doc):
    # the dual-axis synthetic has NO legend at all -> text tier cannot fire,
    # questions must remain
    result = chart_extractor.extract_charts(dual_doc, 0)
    assert result["status"] == "needs_hint"


def test_legend_style_collision_drops_both_entries(monkeypatch):
    """Two legend entries sharing a stroke color identify nothing: the
    collision-drop path must leave the question open (no text answer)."""
    from pdf_mcp import chart_extractor as ce

    blue = (0.1, 0.2, 0.8)
    monkeypatch.setattr(
        ce,
        "_legend_entries",
        lambda page, panel: [
            ((blue, None, 1.0), "series alpha"),
            ((blue, None, 1.0), "series beta"),
        ],
    )
    monkeypatch.setattr(
        ce,
        "_axis_titles",
        lambda page, panel: {"left": "alpha (units)", "right": "beta (units)"},
    )
    curves = [{"_style_key": (blue, None, 1.0), "points": [[0, 0], [1, 1]]}]
    questions = [{"id": "p0.s0.axis", "kind": "y_axis_for_curve"}]
    answers, labels = ce.resolve_semantics(None, None, curves, questions)
    assert answers == {}, "collision must disable text self-answer"
    assert labels == {}


def test_legend_unique_match_answers(monkeypatch):
    from pdf_mcp import chart_extractor as ce

    blue = (0.1, 0.2, 0.8)
    red = (0.9, 0.1, 0.1)
    monkeypatch.setattr(
        ce,
        "_legend_entries",
        lambda page, panel: [
            ((blue, None, 1.0), "series alpha"),
            ((red, None, 1.0), "series beta"),
        ],
    )
    monkeypatch.setattr(
        ce,
        "_axis_titles",
        lambda page, panel: {"left": "alpha (units)", "right": "beta (units)"},
    )
    curves = [
        {"_style_key": (blue, None, 1.0), "points": [[0, 0], [1, 1]]},
        {"_style_key": (red, None, 1.0), "points": [[0, 0], [1, 1]]},
    ]
    questions = [
        {"id": "p0.s0.axis", "kind": "y_axis_for_curve"},
        {"id": "p0.s1.axis", "kind": "y_axis_for_curve"},
    ]
    answers, labels = ce.resolve_semantics(None, None, curves, questions)
    assert answers == {"p0.s0.axis": "left", "p0.s1.axis": "right"}


def test_colorbar_never_wins_as_y_axis():
    """Regression for the arXiv 2001.08361 p24 Fig18 wrong-emit: a compact
    3-tick panel y-axis (100/300/500, pixel span < 60) sits next to a taller
    ScalarMappable colorbar (0..10, "Test Loss"). Pre-fix, the compact real
    axis was rejected by the 60pt span filter and the colorbar's own tick
    column won as the right-side y-axis by default, calibrating the two real
    line series (ground truth y in 150..480) against the colorbar's 0..10
    scale instead — a ~100x-smaller chimera range, easily detectable.

    Both fix layers are exercised: (1) the lowered 45pt span threshold admits
    the real compact axis so it out-competes the colorbar as a `lefts`
    candidate, and (2) `_looks_like_colorbar` rejects the colorbar strip
    outright so it can never become a `rights` candidate even if the real
    axis were absent.
    """
    doc = pymupdf.open(SYN / "line_colorbar.pdf")
    result = chart_extractor.extract_charts(doc, 0, max_points=12)
    doc.close()
    assert result["status"] == "ok"
    chart = result["charts"][0]
    assert chart["y_axis"]["side"] == "left", "must calibrate off the panel's own axis"
    curves = chart["curves"]
    assert len(curves) == 2, "both real line series must be emitted"
    for curve in curves:
        ys = [p[1] for p in curve["points"]]
        # ground-truth range is 150..480; colorbar range is 0..10 — a
        # chimera would land entirely below 10, so this range check alone
        # distinguishes a correct emission from the wrong-emit.
        assert min(ys) > 50, f"y-values too low, looks colorbar-calibrated: {ys}"
        assert max(ys) < 600, f"y-values out of the real axis range: {ys}"


def test_style_is_uniform_dict_shape_across_kinds():
    """#1: every kind (curve/bar/scatter) emits the SAME style shape:
    {"color": [r,g,b]|None, "width": float} — never a raw tuple/string."""

    def check_style(style):
        assert isinstance(style, dict)
        assert set(style) == {"color", "width", "dash"}
        assert style["dash"] is None or isinstance(style["dash"], str)
        assert style["color"] is None or (
            isinstance(style["color"], list) and len(style["color"]) == 3
        )
        assert isinstance(style["width"], float)

    doc = pymupdf.open(SYN / "line_color_linear.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    for c in r["charts"][0]["curves"]:
        check_style(c["style"])

    doc = pymupdf.open(SYN / "bar_simple.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    bar_charts = [ch for ch in r["charts"] if ch.get("bars")]
    assert bar_charts
    for s in bar_charts[0]["bars"]:
        check_style(s["style"])

    doc = pymupdf.open(SYN / "scatter_simple.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    scatter_charts = [ch for ch in r["charts"] if ch.get("points")]
    assert scatter_charts
    for s in scatter_charts[0]["points"]:
        check_style(s["style"])


def test_series_fields_present_with_null_across_kinds():
    """#5: every series carries the same optional fields, present-with-null,
    regardless of kind — bars/scatter get multivalued=False,
    downsampled=False, n_extrema_dropped=0 rather than omitting them."""
    common = {
        "style",
        "label",
        "axis",
        "resolved_by",
        "multivalued",
        "downsampled",
        "n_extrema_dropped",
    }

    doc = pymupdf.open(SYN / "bar_simple.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    bar_charts = [ch for ch in r["charts"] if ch.get("bars")]
    for s in bar_charts[0]["bars"]:
        assert common <= set(s)
        assert s["multivalued"] is False
        assert s["downsampled"] is False
        assert s["n_extrema_dropped"] == 0
        assert "bars" in s

    doc = pymupdf.open(SYN / "scatter_simple.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    scatter_charts = [ch for ch in r["charts"] if ch.get("points")]
    for s in scatter_charts[0]["points"]:
        assert common <= set(s)
        assert s["multivalued"] is False
        assert s["downsampled"] is False
        assert s["n_extrema_dropped"] == 0
        assert "points" in s


def test_diagnostics_always_carries_notes_key():
    """#5/#6: diagnostics.notes is always present (possibly empty) — it
    should never be missing regardless of chart kind."""
    for fname in ("bar_simple.pdf", "scatter_simple.pdf", "line_color_linear.pdf"):
        doc = pymupdf.open(SYN / fname)
        r = chart_extractor.extract_charts(doc, 0)
        doc.close()
        for ch in r["charts"]:
            assert "notes" in ch["diagnostics"]
            assert isinstance(ch["diagnostics"]["notes"], list)


def test_axis_titles_and_range_populated(line_doc):
    """#2: x_axis/y_axis carry title (str|None) and range ([min,max])."""
    r = chart_extractor.extract_charts(line_doc, 0)
    chart = r["charts"][0]
    for axis_key in ("x_axis", "y_axis"):
        axis = chart[axis_key]
        assert "title" in axis
        assert axis["title"] is None or isinstance(axis["title"], str)
        assert "range" in axis
        lo, hi = axis["range"]
        assert isinstance(lo, float) and isinstance(hi, float)
        assert lo < hi


def test_curve_label_populated_from_legend_without_dual_axis():
    """#2: label matching runs for every curve, not only when a dual-axis
    question was asked. line_two_legend_dashed.pdf has a legend and a
    single y-axis (no dual-axis ambiguity)."""
    doc = pymupdf.open(SYN / "line_two_legend_dashed.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    assert r["status"] == "ok"
    labels = [c.get("label") for ch in r["charts"] for c in ch.get("curves", [])]
    assert any(lab is not None for lab in labels), "expected a legend-matched label"


def test_points_rounded_to_four_sig_figs(line_doc):
    """#3: emitted point values are rounded to 4 significant figures — no
    fictional 15-digit float precision."""
    r = chart_extractor.extract_charts(line_doc, 0)
    for c in r["charts"][0]["curves"]:
        for x, y in c["points"]:
            for v in (x, y):
                mantissa = f"{abs(v):.10e}".split("e")[0]
                digits = mantissa.replace(".", "").rstrip("0") or "0"
                assert len(digits) <= 4, f"value {v} exceeds 4 sig figs ({digits})"


def test_sig_helper():
    assert chart_extractor._sig(1361412345678901.0) == 1.361e15
    assert chart_extractor._sig(0.000123456) == 0.0001235
    assert chart_extractor._sig(5.0) == 5.0


def test_verification_card_present_and_shaped(line_doc):
    """FR1: every emitted (non-declined) chart carries a verification_card —
    the reading the heuristics made, render-comparable in one glance: axis
    scale/range/ticks (raw text + parsed value), and the series color↔label
    map with coarse hue words. Exact-RGB is retained and a color_names_unique
    flag signals when two series share a hue word (FR1 collision caveat)."""
    result = chart_extractor.extract_charts(line_doc, 0)
    assert result["status"] == "ok"
    card = result["charts"][0]["verification_card"]
    for axk in ("x_axis", "y_axis"):
        ax = card[axk]
        assert ax["scale"] in ("linear", "log")
        assert len(ax["range"]) == 2
        assert ax["ticks"], f"{axk} must list the ticks it read"
        for t in ax["ticks"]:
            assert set(t) == {"raw", "value"}
            assert isinstance(t["raw"], str)
            assert isinstance(t["value"], float)
    assert isinstance(card["color_names_unique"], bool)
    for s in card["series"]:
        assert set(s) >= {"color", "color_name", "dash", "label"}
        assert s["color"] is None or len(s["color"]) == 3
        assert s["color_name"] is None or isinstance(s["color_name"], str)


def test_verification_default_unverified(line_doc):
    """FR2: an emitted chart is `unverified` until a caller records a verdict."""
    result = chart_extractor.extract_charts(line_doc, 0)
    assert result["charts"][0]["verification"] == "unverified"


def test_verify_confirmed_sets_card_confirmed(line_doc):
    """FR3: `p{n}.verify=confirmed` records that a caller asserted the reading
    is correct — verification becomes card_confirmed (asserted, not attested;
    the coordinates were already engine-trust and are unchanged)."""
    result = chart_extractor.extract_charts(line_doc, 0, {"p0.verify": "confirmed"})
    ch = result["charts"][0]
    assert ch["verification"] == "card_confirmed"
    assert ch["curves"][0]["points"], "coordinates unchanged by confirmation"


def test_verify_labels_wrong_nulls_all_labels_keeps_coordinates():
    """FR3: `labels_wrong` (no series id) keeps the exact coordinates but
    nulls every legend-derived label with resolved_by caller_rejected — the
    table survives with honest anonymity (the whole-legend Mamba case)."""
    doc = pymupdf.open(SYN / "line_two_legend_dashed.pdf")
    result = chart_extractor.extract_charts(doc, 0, {"p0.verify": "labels_wrong"})
    doc.close()
    ch = result["charts"][0]
    assert ch["verification"] == "labels_rejected"
    assert ch["curves"], "coordinates must survive a label rejection"
    for c in ch["curves"]:
        assert c["points"], "points kept"
        assert c["label"] is None
        assert c["resolved_by"] == "caller_rejected"


def test_verify_labels_wrong_per_series_keeps_the_rest():
    """FR3 per-series reject: `labels_wrong:s0` nulls only series 0's label;
    other series keep their engine-read labels (closed grammar, no free-text,
    avoids the whole-legend over-punishment when only one label is wrong)."""
    doc = pymupdf.open(SYN / "line_two_legend_dashed.pdf")
    base = chart_extractor.extract_charts(doc, 0)
    n_curves = len(base["charts"][0]["curves"])
    result = chart_extractor.extract_charts(doc, 0, {"p0.verify": "labels_wrong:s0"})
    doc.close()
    if n_curves < 2:
        pytest.skip("fixture needs >=2 curves to test selective reject")
    ch = result["charts"][0]
    assert ch["verification"] == "labels_rejected"
    assert ch["curves"][0]["label"] is None
    assert ch["curves"][0]["resolved_by"] == "caller_rejected"
    assert any(c["label"] is not None for c in ch["curves"][1:]), "others kept"


def test_verify_axes_wrong_terminal_declines_in_phase1(line_doc):
    """FR3 phase-1 routing: `axes_wrong` has no calibration target yet (that
    is v18), so it terminal-declines with the caller-rejected reason rather
    than emitting an axis the caller says is misread."""
    result = chart_extractor.extract_charts(line_doc, 0, {"p0.verify": "axes_wrong"})
    ch = result["charts"][0]
    assert ch["chart_type"] == "declined"
    assert "rejected axis reading" in ch["decline_reason"]
    assert not ch.get("curves")


def test_verify_invalid_value_errors(line_doc):
    """A verify verdict outside the closed enum is a validation error, like
    the other closed-enum hint suffixes."""
    r = chart_extractor.extract_charts(line_doc, 0, {"p0.verify": "maybe"})
    assert "error" in r


def test_verification_card_color_names_unique_flag(monkeypatch):
    """color_names_unique is False when >=2 emitted series coarse-name to the
    same hue word (the palette-collision case where the card alone can't
    disambiguate and the caller needs the render swatches)."""
    doc = pymupdf.open(SYN / "line_two_legend_dashed.pdf")
    result = chart_extractor.extract_charts(doc, 0)
    doc.close()
    card = result["charts"][0]["verification_card"]
    names = [s["color_name"] for s in card["series"]]
    expected_unique = len(names) == len(set(names))
    assert card["color_names_unique"] == expected_unique


def test_axis_verify_reason_flags_exponent_reads():
    """Precise-flag: an axis whose ticks were read from superscript/exponent
    geometry (raw carries '^': 10^k, 2^k, drawn-minus) is the historically
    catastrophic class — r2 can't separate good from bad reads there (both
    fit ~1.0), so the PATH is the trigger. A clean plain-text axis is not
    flagged."""
    r = chart_extractor._axis_verify_reason(1.0, ["2^19", "2^20", "2^21"])
    assert r is not None and "exponent" in r.lower()
    assert chart_extractor._axis_verify_reason(1.0, ["0", "2", "4", "6"]) is None


def test_power_axis_emits_verify_flag_clean_axis_does_not():
    """Integration: a chart with a 2^k / 10^k axis carries a `verify` flag on
    that axis (the precise, rare flag), while a clean linear chart carries no
    verify flag on either axis."""
    doc = pymupdf.open(SYN / "line_log2.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    doc.close()
    ch = next(c for c in r["charts"] if c.get("curves"))
    assert "verify" in ch["x_axis"] and "exponent" in ch["x_axis"]["verify"]
    assert "verify" not in ch["y_axis"]  # y is a clean linear axis here

    doc = pymupdf.open(SYN / "line_color_linear.pdf")
    r2 = chart_extractor.extract_charts(doc, 0)
    doc.close()
    ch2 = r2["charts"][0]
    assert "verify" not in ch2["x_axis"] and "verify" not in ch2["y_axis"]


def test_axis_verify_reason_flags_marginal_fit():
    """A marginal calibration fit (low r2) is a distinct uncertainty and is
    flagged even on plain ticks; a clean fit (r2 ~1.0) is not."""
    r = chart_extractor._axis_verify_reason(0.982, ["0", "10", "20"])
    assert r is not None and "fit" in r.lower()
    assert chart_extractor._axis_verify_reason(0.9999, ["0", "10", "20"]) is None


def test_color_name_coarse_hue_words():
    """The verification card needs a coarse hue word so a caller can compare
    the legend map to the render without color-picking. Primary/secondary
    hues and the neutrals must name predictably."""
    cn = chart_extractor._color_name
    assert cn((0.84, 0.15, 0.16)) == "red"  # matplotlib tab:red
    assert cn((0.12, 0.47, 0.71)) == "blue"  # matplotlib tab:blue
    assert cn((1.0, 0.6, 0.0)) == "orange"
    assert cn((0.17, 0.63, 0.17)) == "green"
    assert cn((0.0, 0.0, 0.0)) == "black"
    assert cn((1.0, 1.0, 1.0)) == "white"
    assert cn((0.5, 0.5, 0.5)) == "gray"
    assert cn(None) is None


def test_looks_like_colorbar_detects_raster_strip(dual_doc):
    """`_looks_like_colorbar` is a standalone geometry check: a raster image
    (or dense stack of thin filled rects) sitting in a narrow band between
    the x-axis span's right edge and a candidate y-axis column reads as a
    colorbar. Exercise it directly against a page with no such raster band
    (the ordinary dual-axis synthetic fixture) to confirm it returns False
    absent colorbar-shaped content, so the helper isn't a rubber stamp."""
    page = dual_doc[0]
    panel = chart_extractor.find_panels(page)[0]
    x1 = panel["xa"]["px"].max()
    ya = panel["ya"]
    assert chart_extractor._looks_like_colorbar(page, x1, ya) is False


def test_looks_like_axis_title():
    good = [
        "size",
        "Superposition Size",
        "Diode Reverse Voltage (V)",
        "Compute (PF-days)",
        "Number of d electrons",
        "iter. (1e4)",
    ]
    bad = [
        "as follows. In the multi-speaker task, we used en.base and",
        "Figure 2 | Training curve envelope for a range of model sizes.",
    ]
    for t in good:
        assert chart_extractor._looks_like_axis_title(t) is True, t
    for t in bad:
        assert chart_extractor._looks_like_axis_title(t) is False, t


def test_axis_title_rejects_body_text():
    """Ship-blocker regression: _x_axis_title must never launder body text
    or a figure caption into x_axis.title. 1807.11632 p4 (0-indexed 3) is a
    real sample where the nearest horizontal line under the panel used to be
    a sentence fragment, not the true axis title ("size")."""
    doc = pymupdf.open(REAL / "1807.11632.pdf")
    result = chart_extractor.extract_charts(doc, 3)
    doc.close()
    chart = result["charts"][0]
    # positive: the REAL x-title is captured (not null, not the body-text
    # pollution "as follows. In the multi-speaker task, ...")
    assert chart["x_axis"]["title"] == "size"
    # the left + right y-axis titles are the real ones, not captions
    assert chart["y_axis"]["title"] == "MCD (dB)"
    assert "y_axis_right" in chart
    assert "RMSE" in (chart["y_axis_right"]["title"] or "")


def test_base2_log_axis_recovered_not_read_as_linear():
    """Base-2 superscript ticks (2^19..2^27) must read as a LOG axis, not glue
    to integers "219..227" and emit a linear axis off by orders of magnitude
    (verified wrong-emit on Hestness 1712.00409)."""
    doc = pymupdf.open(SYN / "line_log2.pdf")
    result = chart_extractor.extract_charts(doc, 0)
    doc.close()
    xa = result["charts"][0]["x_axis"]
    assert xa["scale"] == "log"
    assert xa["range"][0] == 2**19 and xa["range"][1] == 2**27


def test_negative_decade_log_axis_recovered_not_linear():
    """Negative-decade superscript ticks (10^-4..10^0, matplotlib mathtext)
    must read as a LOG axis. Two real wrong-emits in this class: SGDR
    1608.03983 ("Learning rate" emitted as linear [-4, 0] — the kerned
    exponent OVERLAPS its base by ~0.007pt and the pairing gate's 0-floor
    rejected the pair) and Henighan 2010.14701 (vector-drawn minus, declines).
    The committed fixture has a typed minus, so the correct outcome is a READ;
    the invariant either way is: never a linear [-4, 0] axis."""
    doc = pymupdf.open(SYN / "line_logneg.pdf")
    result = chart_extractor.extract_charts(doc, 0)
    doc.close()
    ch = result["charts"][0]
    assert ch["chart_type"] != "declined", ch.get("decline_reason")
    ya = ch["y_axis"]
    assert ya["scale"] == "log"
    assert ya["range"] == [0.0001, 1.0]


def test_superscript_pairing_requires_vertical_overlap():
    """The -2pt overlap tolerance must NOT pair vertically-distant spans: on
    2607.08500 p25 an x-tick "-3" paired with a body-text "2" sitting 88pt
    BELOW it (bogus 2^-3), eating the tick and breaking a genuinely-linear
    [-3, 3] axis. A superscript's vertical band overlaps its base's."""
    pdf = REAL / "2607.08500.pdf"
    if not pdf.exists():
        pytest.skip("real corpus not fetched")
    doc = pymupdf.open(pdf)
    paired, _, _ = chart_extractor._power_pairs(doc[24])
    result = chart_extractor.extract_charts(doc, 24)
    doc.close()
    for s in paired:
        bb = s["bb"]
        assert bb[3] - bb[1] < 20, f"vertically-smeared pair {s['raw']} {bb}"
    ranges = [c["x_axis"]["range"] for c in result["charts"]]
    assert ranges == [[-3.0, 3.0], [-3.0, 3.0]], ranges


def test_sgdr_learning_rate_axis_reads_log():
    """Real-PDF regression for the pairing-gate fix (kerned overlap)."""
    pdf = REAL / "1608.03983.pdf"
    if not pdf.exists():
        pytest.skip("real corpus not fetched")
    doc = pymupdf.open(pdf)
    result = chart_extractor.extract_charts(doc, 1)
    doc.close()
    ch = result["charts"][0]
    assert ch["chart_type"] != "declined"
    assert ch["y_axis"]["scale"] == "log"
    assert ch["y_axis"]["range"] == [0.0001, 1.0]


def test_henighan_drawn_minus_axis_reads_negative():
    """Stage-3 drawn-minus READING: Fig 16's x-axis is 10^-6..10^1 PF-days
    with the exponent minus drawn as an hrule (not a glyph). v6-v8 declined
    (detect-only); v9 reads the bar in the base->exponent gap and negates the
    exponent — the axis must emit as log [1e-06, 10], never the sign-dropped
    [1, 1e6] (the original catastrophic wrong-emit) and no longer a decline."""
    pdf = REAL / "2010.14701.pdf"
    if not pdf.exists():
        pytest.skip("real corpus not fetched")
    doc = pymupdf.open(pdf)
    result = chart_extractor.extract_charts(doc, 21)
    doc.close()
    emitting = [c for c in result["charts"] if c.get("curves")]
    assert emitting, "Fig 16 panels must emit now"
    for ch in emitting:
        assert ch["x_axis"]["scale"] == "log"
        # each panel's compute axis starts in the negative decades (10^-6 to
        # 10^-4 depending on panel) — a sign-dropped read starts >= 1
        assert ch["x_axis"]["range"][0] < 1.0, ch["x_axis"]["range"]
        assert ch["x_axis"]["range"][1] <= 100.0


def test_grazing_error_bar_cap_does_not_negate():
    """The drawn-minus reader must not negate a POSITIVE tick because a data
    element grazes the tick bbox (2607.06360 p20: error-bar cap 0.2pt above
    a 10^36 label — the strictly-inside band excludes it)."""
    pdf = REAL / "2607.06360.pdf"
    if not pdf.exists():
        pytest.skip("real corpus not fetched")
    doc = pymupdf.open(pdf)
    result = chart_extractor.extract_charts(doc, 19)
    doc.close()
    for ch in result["charts"]:
        for ax in (ch["x_axis"], ch["y_axis"]):
            rng = ax.get("range")
            if rng and ax["scale"] == "log":
                assert rng[0] >= 1.0, f"negated a positive axis: {rng}"


def test_drawn_glyph_labels_get_specific_decline_reason():
    """A SuperMongo/PGPLOT-style figure draws EVERYTHING as strokes — axis
    frame, tick marks, and the tick-label digits (Hershey fonts / outlined
    text). Nothing lands in the text layer, so no panel can form; but the
    generic 'no chart signature' reason reads as 'not a chart', sending the
    consumer hunting for a tool bug. Axis-like frame geometry with a
    text-empty label zone must name the real situation: unreadable labels,
    use the render. (Blanton astro-ph/0210215 p33 is the wild sample.)"""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    # stroked axis frame + tick marks, zero text anywhere
    page.draw_rect(pymupdf.Rect(60, 40, 360, 240), color=(0, 0, 0), width=1)
    for i in range(6):
        x = 60 + i * 60
        page.draw_line((x, 240), (x, 234), color=(0, 0, 0), width=0.8)
        y = 40 + i * 40
        page.draw_line((60, y), (66, y), color=(0, 0, 0), width=0.8)
    # a data polyline inside the frame
    pts = [(60 + i * 30, 220 - i * 15) for i in range(11)]
    for a, b in zip(pts, pts[1:]):
        page.draw_line(a, b, color=(0, 0, 1), width=1.2)
    result = chart_extractor.extract_charts(doc, 0)
    doc.close()
    assert result["status"] == "declined"
    assert any("no readable tick-label text" in r for r in result["reasons"]), result[
        "reasons"
    ]


def test_textless_prose_page_keeps_generic_decline_reason():
    """A page with no axis-like geometry (prose, or a lone header rule) must
    keep the generic no-chart-signature reason — the drawn-glyph wording is
    reserved for pages that actually carry frame geometry."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((60, 60), "Just some body text, nothing chart-like.")
    page.draw_line((40, 80), (360, 80), color=(0, 0, 0), width=0.5)
    result = chart_extractor.extract_charts(doc, 0)
    doc.close()
    assert result["status"] == "declined"
    assert any("no chart signature" in r for r in result["reasons"]), result["reasons"]


class _StubPage:
    """Minimal page double for _power_pairs: crafted rawdict + no drawings."""

    def __init__(self, spans):
        self._spans = spans

    def get_drawings(self):
        return []

    def get_text(self, kind):
        assert kind == "rawdict"
        return {
            "blocks": [
                {
                    "lines": [
                        {
                            "spans": [
                                {
                                    "size": size,
                                    "bbox": bbox,
                                    "chars": [{"c": c} for c in txt],
                                }
                            ]
                        }
                        for txt, size, bbox in self._spans
                    ]
                }
            ]
        }


def test_superscript_raise_gate_tolerates_small_font_metrics():
    """FlashAttention 2205.14135 p9: at 8pt figure fonts the superscript's
    bbox top sits 0.51pt BELOW the base's top — the raise gate's
    `top < base_top + 0.5` missed the pair by 0.01pt, the labels glued to
    '100'/'101'/'102' in the words layer, and the log y-axis emitted as
    linear [100, 102] (r2=0.9997) — runtimes compressed to a meaningless
    ~100ms band. Exact geometry from the wild page; the pair must recover."""
    page = _StubPage(
        [
            ("10", 8.0, [116.41, 108.75, 124.62, 118.37]),
            ("1", 4.66, [124.61, 109.26, 127.01, 114.87]),
        ]
    )
    sup = chart_extractor.superscript_powers(page)
    assert [s["v"] for s in sup] == [10.0], sup


def test_superscript_raise_gate_still_rejects_baseline_and_subscript():
    """The relaxed raise test must not admit non-superscript neighbors:
    a same-baseline smaller digit (bottom flush with the base's) and a
    subscript (bottom below the base's) both stay unpaired."""
    baseline = _StubPage(
        [
            ("10", 8.0, [100.0, 100.0, 108.0, 110.0]),
            ("5", 6.0, [108.5, 102.5, 111.0, 110.0]),  # bottom flush
        ]
    )
    assert chart_extractor.superscript_powers(baseline) == []
    subscript = _StubPage(
        [
            ("10", 8.0, [100.0, 100.0, 108.0, 110.0]),
            ("2", 4.66, [108.0, 106.0, 110.4, 112.5]),  # bottom below base
        ]
    )
    assert chart_extractor.superscript_powers(subscript) == []


def test_flashattention_log_y_axis_reads_log_not_linear():
    """Integration pin for the wild wrong-emit: Figure 3 left (p9) must
    calibrate y as log spanning decades 10^0..10^2, never linear [100, 102]."""
    pdf = REAL / "2205.14135.pdf"
    if not pdf.exists():
        pytest.skip("real corpus not fetched")
    doc = pymupdf.open(pdf)
    result = chart_extractor.extract_charts(doc, 8)
    doc.close()
    emitting = [c for c in result["charts"] if c.get("curves")]
    assert emitting, "Fig 3 left must emit"
    for ch in emitting:
        assert ch["y_axis"]["scale"] == "log", ch["y_axis"]
        assert ch["y_axis"]["range"] == [1.0, 100.0], ch["y_axis"]


def test_glued_decade_backstop_declines_linear_calibration():
    """Defense in depth: when an adjacent smaller-digit pair fails even the
    relaxed pairing geometry (unknown future typography), the glued word
    must poison a linear calibration built on it — same contract as the
    orphan-exponent guard."""
    # exponent top BELOW base mid: fails pairing, must become a suspect
    spans = []
    toks = []
    for i, y in enumerate((100.0, 140.0, 180.0)):
        spans.append(("10", 8.0, [100.0, y, 108.0, y + 10.0]))
        spans.append((str(i), 4.66, [108.2, y + 5.5, 110.6, y + 8.5]))
        toks.append(
            {
                "v": 100.0 + i,
                "bb": (100.0, y, 110.6, y + 10.0),
                "raw": f"10{i}",
            }
        )
    page = _StubPage(spans)
    assert chart_extractor.superscript_powers(page) == []
    _, _, suspects = chart_extractor._power_pairs(page)
    assert len(suspects) == 3, suspects
    ax = {"scale": "linear", "toks": toks}
    why = chart_extractor._ticks_unreadable(ax, [], [], [], suspects)
    assert why is not None and "could not be paired" in why, why


def test_comma_decimal_locale_axis_reads_correctly():
    """Round-8 locale pass: unambiguous German comma-decimal tick labels
    ("0,5".."2,0", 1-2 decimal digits) must normalize and EMIT y [0.5, 2.0]
    — the ambiguous formats ("5.000", "1,000", "1 000", "1'000") are locale
    gates that decline and are covered by line_locale_de + the attack set
    (all adjudicated declining, no wrong-emit; RESULTS.md v15 ship pass)."""
    doc = pymupdf.open(SYN / "line_locale_de_decimal.pdf")
    result = chart_extractor.extract_charts(doc, 0)
    doc.close()
    assert result["status"] == "ok", (result["status"], result["reasons"])
    ch = result["charts"][0]
    assert ch["y_axis"]["range"] == [0.5, 2.0], ch["y_axis"]
    assert any(c.get("points") for c in ch.get("curves", []))


def test_legend_entries_tight_pitch_no_off_by_one():
    """Mamba 2312.00752 p15 Fig 8: legend rows at ~7.1pt pitch, sample
    strokes at each row's center. The old pairing window ([y0-4, y1+4],
    first-match-in-draw-order) let row k's sample also satisfy row k+1, so
    'Convolution' claimed the blue FlashAttention-2 sample; the unique-color
    filter then dropped blue entirely (label None) and every other curve
    wore the label ONE ROW OFF — 'Scan (ours)' (the paper's contribution)
    read as 'OOM'. Samples must pair by nearest row-center with the center
    inside the row band. Exact wild geometry."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    rows = [
        (388.0, "FlashAttention-2", (0.28, 0.47, 0.82)),
        (394.9, "Convolution", (0.93, 0.52, 0.29)),
        (402.0, "Scan (PyTorch)", (0.42, 0.80, 0.39)),
        (409.2, "Scan (ours)", (0.84, 0.37, 0.37)),
    ]
    for y0, label, color in rows:
        # sample stroke at the row's visual center (wild: cy = y0 + 3.4)
        page.draw_line((108.3, y0 + 3.4), (117.8, y0 + 3.4), color=color)
        page.insert_text((121.6, y0 + 5.5), label, fontsize=6.3)
    panel = {"rx0": 90.0, "rx1": 320.0, "ry0": 370.0, "ry1": 440.0}
    entries = chart_extractor._legend_entries(page, panel)
    doc.close()
    got = {lab: st[0] for st, lab in entries}
    assert len(entries) == 4, entries
    for _, label, color in rows:
        assert label in got, f"{label} missing: {entries}"
        assert got[label] == tuple(round(c, 2) for c in color) or all(
            abs(a - b) < 0.02 for a, b in zip(got[label], color)
        ), (label, got[label], color)


def test_mamba_fig8_labels_not_shifted():
    """Integration pin for the wild mislabel: every emitted curve's label
    must match its own color's legend entry — the blue (steepest,
    FlashAttention-2) curve must not be None, and no curve may wear the
    marker-only 'OOM' annotation entry as its label."""
    pdf = REAL / "2312.00752.pdf"
    if not pdf.exists():
        pytest.skip("real corpus not fetched")
    doc = pymupdf.open(pdf)
    result = chart_extractor.extract_charts(doc, 14)
    doc.close()
    want = {
        (0.28, 0.47, 0.82): "FlashAttention-2",
        (0.93, 0.52, 0.29): "Convolution",
        (0.42, 0.8, 0.39): "Scan (PyTorch)",
        (0.84, 0.37, 0.37): "Scan (ours)",
    }
    emitting = [c for ch in result["charts"] for c in ch.get("curves") or []]
    assert emitting, "Fig 8 must emit"
    for c in emitting:
        color = tuple(c["style"]["color"])
        expected = want.get(color)
        assert expected is not None, color
        assert c["label"] == expected, (color, c["label"], expected)


def test_base_level_drawn_minus_declines():
    """Base-level drawn minus (Origin/journal typography): tick digits are
    text but every minus sign is a drawn rule (syn corpus doctors a real
    matplotlib chart: minus glyphs redacted, thin filled bars drawn in their
    place). Reading the digits without the sign calibrates a MIRRORED axis
    at r2=1.0 (x [18,24] for a true [-24,-18]) and the only obstacle to
    emission is an incidental chart-type question — which an honest agent
    answers 'line', producing a silent sign-flipped table. The axis-sign
    gate must decline BEFORE any hint round."""
    doc = pymupdf.open(SYN / "line_drawn_minus.pdf")
    result = chart_extractor.extract_charts(doc, 0)
    assert result["status"] == "declined", result["status"]
    ch = result["charts"][0]
    assert ch["chart_type"] == "declined"
    assert "sign" in ch["decline_reason"]
    # the mirrored calibration ([18,24] for [-24,-18]) is known-wrong and
    # must not ride along in the declined chart's axis metadata
    assert ch["x_axis"]["range"] is None
    assert ch["y_axis"]["range"] is None
    # the sanctioned hint path must not resurrect the wrong emit either
    hinted = chart_extractor.extract_charts(doc, 0, {"p0.type": "line"})
    doc.close()
    for c in hinted.get("charts", []):
        assert not c.get("curves"), "sign-flipped curve emitted via hint path"


def test_typed_minus_negative_linear_axis_still_emits():
    """Control for the base-level drawn-minus gate: the SAME chart with
    ordinary typed minus glyphs must keep emitting with correct negative
    ranges — a typed minus on the axis proves the toolchain types signs,
    so the gate must stay off."""
    doc = pymupdf.open(SYN / "line_neg_linear.pdf")
    result = chart_extractor.extract_charts(doc, 0)
    doc.close()
    assert result["status"] == "ok", (result["status"], result["reasons"])
    ch = result["charts"][0]
    assert ch["x_axis"]["range"] == [-24.0, -18.0], ch["x_axis"]["range"]
    assert ch["y_axis"]["range"][0] < 0, ch["y_axis"]["range"]
    assert any(c.get("points") for c in ch.get("curves", []))


def test_sparse_marker_line_recovered():
    """A 5-point marker line (one point per model size — the canonical
    scaling-law figure) is below the dense-cloud gate (>=8 vertices) but must
    classify and extract via marker-vertex coincidence, with exact values and
    a sparse-capture honesty note. Pre-v7 it fell through to 'unknown' and
    emitted nothing."""
    doc = pymupdf.open(SYN / "line_sparse.pdf")
    result = chart_extractor.extract_charts(doc, 0)
    doc.close()
    ch = result["charts"][0]
    assert ch["chart_type"] == "line"
    assert len(ch["curves"]) == 1
    assert ch["curves"][0]["points"] == [
        [1.0, 61.0],
        [2.0, 67.0],
        [4.0, 72.0],
        [8.0, 74.5],
        [16.0, 76.0],
    ]
    assert any("sparse line capture" in n for n in ch["diagnostics"]["notes"])


def test_no_vector_geometry_declines_with_reason():
    """A panel whose interior holds (essentially) no vector geometry —
    rasterized plot data, or a phantom axis pairing with a stray vertex —
    must DECLINE with the rasterized/unsupported reason instead of asking a
    chart_type question no answer can satisfy. Page 21 mixes both decline
    classes: the positive-axis panels carry the no-geometry reason, while
    the negative-dB panels hit the (earlier) base-level drawn-minus sign
    gate — their axis metadata is sign-flipped, so the sign reason must win
    for them."""
    pdf = REAL / "2607.03442.pdf"
    if not pdf.exists():
        pytest.skip("real corpus not fetched")
    doc = pymupdf.open(pdf)
    result = chart_extractor.extract_charts(doc, 20)
    doc.close()
    assert result["charts"], "panels must still be reported"
    reasons = []
    for ch in result["charts"]:
        assert ch["chart_type"] == "declined"
        reasons.append(ch["decline_reason"])
    assert any("no extractable vector plot geometry" in r for r in reasons)
    assert any("sign is drawn, not typed" in r for r in reasons)
    assert result["status"] == "declined"


def test_base_level_drawn_minus_real_corpus_declines():
    """2607.03442 p31 (phase-noise figure, y −70..−20 dBc/Hz): tick digits
    are text, every minus is a vector-drawn filled rule. Pre-gate this page
    EMITTED both curves against a sign-flipped y axis ([20, 70]) — the
    first confirmed wild wrong-emit of the base-level drawn-minus class.
    Must decline with the sign reason and emit nothing."""
    pdf = REAL / "2607.03442.pdf"
    if not pdf.exists():
        pytest.skip("real corpus not fetched")
    doc = pymupdf.open(pdf)
    result = chart_extractor.extract_charts(doc, 30)
    doc.close()
    assert result["charts"]
    for ch in result["charts"]:
        assert ch["chart_type"] == "declined", ch
        assert "sign is drawn, not typed" in ch["decline_reason"]
        assert not ch.get("curves")
        assert ch["y_axis"]["range"] is None, "flipped range leaked"


def test_bar_misclassification_falls_back_to_question():
    """Large OPEN markers misread as bar rects (astro scatter squares) used
    to return a typed-but-empty 'bar' chart; with zero baseline series the
    classification is untrustworthy — fall back to the chart_type question."""
    pdf = REAL / "2607.06338.pdf"
    if not pdf.exists():
        pytest.skip("real corpus not fetched")
    doc = pymupdf.open(pdf)
    result = chart_extractor.extract_charts(doc, 6)
    doc.close()
    empty_bar = [
        c for c in result["charts"] if c["chart_type"] == "bar" and not c.get("bars")
    ]
    assert not empty_bar, "typed-but-empty bar chart must not be returned"
    assert any(
        q["kind"] == "chart_type" for q in result.get("questions", [])
    ), "mis-classified panel must ask the chart_type question"


def test_hinted_type_with_no_series_declines():
    """An EXPLICITLY hinted chart type that still extracts nothing must
    decline with the honest reason, not return an ok-empty typed chart."""
    pdf = REAL / "0811.0781.pdf"
    if not pdf.exists():
        pytest.skip("real corpus not fetched")
    doc = pymupdf.open(pdf)
    r = chart_extractor.extract_charts(doc, 28)
    hints = {
        q["id"]: "scatter" for q in r.get("questions", []) if q["kind"] == "chart_type"
    }
    if hints:
        r = chart_extractor.extract_charts(doc, 28, hints)
    doc.close()
    for ch in r["charts"]:
        n = (
            len(ch.get("curves", []))
            + len(ch.get("bars", []))
            + len(ch.get("points", []))
        )
        if n == 0:
            assert ch["chart_type"] == "declined", ch
            assert ch.get("decline_reason"), ch


def test_hinted_line_does_not_emit_bracket_decoy():
    """A chart_type='line' HINT confirms the chart, not that every short
    polyline is data: the significance bracket (4 vertices, wide span, tiny
    y-drop) must NOT emit as a curve. The real sparse line here carries no
    markers, so nothing is extractable — the panel declines with the honest
    hinted-type reason (adversarial review probe, v7 fix wave)."""
    doc = pymupdf.open(SYN / "line_bracket_decoy.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    hints = {
        q["id"]: "line" for q in r.get("questions", []) if q["kind"] == "chart_type"
    }
    assert hints, "fixture must ask the chart_type question first"
    r2 = chart_extractor.extract_charts(doc, 0, hints)
    doc.close()
    for ch in r2["charts"]:
        assert not ch.get("curves"), "bracket decoy must not emit as a curve"
        assert ch["chart_type"] == "declined"
        assert "hinted type 'line'" in ch["decline_reason"]


def test_hinted_scatter_drops_arrowhead_pair():
    """A chart_type='scatter' HINT lowers the marker minimum to 3, not 2:
    the two same-color annotation arrowheads must stay out; only the real
    4-point series emits (adversarial review probe, v7 fix wave)."""
    doc = pymupdf.open(SYN / "scatter_arrow_decoy.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    hints = {
        q["id"]: "scatter" for q in r.get("questions", []) if q["kind"] == "chart_type"
    }
    assert hints, "fixture must ask the chart_type question first"
    r2 = chart_extractor.extract_charts(doc, 0, hints)
    doc.close()
    series = [s for ch in r2["charts"] for s in ch.get("points", [])]
    assert len(series) == 1, f"only the real series may emit, got {len(series)}"
    assert len(series[0]["points"]) == 4


def test_not_a_chart_answer_is_terminal():
    """Answering the chart_type question with 'not_a_chart' must DECLINE the
    panel — pre-fix it fell into the unknown branch and re-asked forever."""
    doc = pymupdf.open(SYN / "line_bracket_decoy.pdf")
    r = chart_extractor.extract_charts(doc, 0)
    hints = {
        q["id"]: "not_a_chart"
        for q in r.get("questions", [])
        if q["kind"] == "chart_type"
    }
    assert hints
    r2 = chart_extractor.extract_charts(doc, 0, hints)
    doc.close()
    assert r2["status"] == "declined"
    assert not r2.get("questions"), "not_a_chart must not re-ask"
    assert all(c["chart_type"] == "declined" for c in r2["charts"])
    assert any("not a chart" in c.get("decline_reason", "") for c in r2["charts"])


def test_same_color_data_and_fit_do_not_merge():
    """Consumer-found (v10): a solid data curve and its same-color DASHED
    power-law fit merged into one interleaved sawtooth series that traced
    neither real curve (multivalued:false — nothing warned the caller), and
    the merged series' beyond-axis fit tail false-declined a clean panel.
    Dash pattern is now part of the style key and surfaced as style.dash;
    log axes get decade-based range margins. Henighan Fig 16: emitted curves
    must be monotone in y (scaling-law data/fits are), and the Text-to-Image
    panel must emit."""
    pdf = REAL / "2010.14701.pdf"
    if not pdf.exists():
        pytest.skip("real corpus not fetched")
    doc = pymupdf.open(pdf)
    result = chart_extractor.extract_charts(doc, 21)
    doc.close()
    emitting = [c for c in result["charts"] if c.get("curves")]
    assert len(emitting) >= 3, "Fig 16 panels (incl. Text-to-Image) must emit"
    for ch in emitting:
        for c in ch["curves"]:
            assert "dash" in c["style"]
            ys = [p[1] for p in c["points"]]
            assert all(
                ys[i] <= ys[i + 1] for i in range(len(ys) - 1)
            ), f"interleaved data+fit sawtooth: {ys}"


def test_title_says_log_predicate():
    f = chart_extractor._title_says_log
    assert f("Number of Tokens (Log-scale)")
    assert f("Frequency (logarithmic)")
    assert f("Compute (log)")
    assert not f("log likelihood")  # a logged QUANTITY, not a scale decl
    assert not f("log loss")
    assert not f("Voltage (V)")
    assert not f(None)
    # word-boundary: "...log scale" inside a longer word must NOT match —
    # Visual Analog Scale is a common LINEAR axis; matching it false-declines.
    assert not f("Visual Analog Scale")
    assert not f("analog scale")
    assert not f("catalog scale")


def test_contradiction_guard_declines_log_title_linear_calibration(monkeypatch):
    """Backstop for unrecoverable log-axis typographies: when an axis title
    DECLARES a log scale but the axis calibrated LINEAR, decline — never emit a
    mis-scaled linear table. NB: this only fires when the log-scale title is a
    short, extractable label; the real Hestness fix is the base^exp reader
    (its actual title is a long sentence the title extractor rejects). Here we
    force a short log-scale x-title onto a genuinely linear chart to exercise
    the guard mechanism directly."""
    monkeypatch.setattr(
        chart_extractor, "_x_axis_title", lambda page, panel: "size (log scale)"
    )
    doc = pymupdf.open(SYN / "line_color_linear.pdf")
    try:
        result = chart_extractor.extract_charts(doc, 0)
    finally:
        doc.close()
    declined = [c for c in result["charts"] if c["chart_type"] == "declined"]
    assert declined, "log-titled linear-calibrated axis must decline"
    assert any("declares a log scale" in c.get("decline_reason", "") for c in declined)


def test_multipanel_ytitle_not_stolen_from_neighbor():
    """Cross-panel title theft (ship-blocker): on a tight 3-panel figure each
    panel's y-title must be ITS OWN. Chinchilla p5 center panel is "Parameters"
    and the right panel is "Tokens" — the right panel used to steal the center
    panel's title (numerically incoherent: tokens labeled parameters)."""
    doc = pymupdf.open(REAL / "2203.15556.pdf")
    result = chart_extractor.extract_charts(doc, 4)
    doc.close()
    titles = [c["y_axis"].get("title") for c in result["charts"]]
    assert "Parameters" in titles and "Tokens" in titles, titles
    # no two panels share the same non-null y-title (theft signature)
    non_null = [t for t in titles if t]
    assert len(non_null) == len(set(non_null)), f"duplicate stolen title: {titles}"


def test_axis_title_null_when_only_body_text_below_axis():
    """Chinchilla p5 has a figure caption under the panel but no real x-axis
    title — the tool must return null, never the caption."""
    doc = pymupdf.open(REAL / "2203.15556.pdf")
    result = chart_extractor.extract_charts(doc, 4)
    doc.close()
    for ch in result["charts"]:
        t = ch["x_axis"]["title"]
        assert t is None or (
            len(t) <= 45 and not t.lower().startswith(("figure", "fig"))
        )


@pytest.mark.skipif(
    not (LOCAL / "littelfuse_sp05.pdf").exists(),
    reason="proprietary datasheet not present locally",
)
def test_real_axis_titles_captured_not_dropped():
    """False-negative guard: the gate must still capture genuine short titles.
    Littelfuse p2 has real x/y titles that the frame-refined-panel bug used to
    drop to null."""
    doc = pymupdf.open(LOCAL / "littelfuse_sp05.pdf")
    result = chart_extractor.extract_charts(doc, 1)
    doc.close()
    chart = result["charts"][0]
    assert chart["x_axis"]["title"] == "Diode Reverse Voltage (V)"
    assert chart["y_axis"]["title"] == "Diode Capacitance (pF)"


def test_dual_axis_exposes_right_axis():
    doc = pymupdf.open(SYN / "line_dual_axis.pdf")
    r1 = chart_extractor.extract_charts(doc, 0)
    hints = {q["id"]: "left" for q in r1["questions"]}
    for q in r1["questions"]:
        if q["series_style"]["color"] and q["series_style"]["color"][0] > 0.5:
            hints[q["id"]] = "right"
    result = chart_extractor.extract_charts(doc, 0, hints=hints)
    doc.close()
    assert result["status"] == "ok"
    chart = result["charts"][0]
    assert "y_axis_right" in chart
    yar = chart["y_axis_right"]
    assert yar["side"] == "right"
    assert len(yar["range"]) == 2
    assert isinstance(yar["range"][0], float) and isinstance(yar["range"][1], float)

    doc2 = pymupdf.open(SYN / "line_color_linear.pdf")
    result2 = chart_extractor.extract_charts(doc2, 0)
    doc2.close()
    assert "y_axis_right" not in result2["charts"][0]
