"""Response-CONTRACT conformance for pdf_extract_chart.

Every bug the external-consumer testing found lived in the response contract —
the *shape* an agent consumes — not in the extracted numbers the benchmarks
already guard:
  - `style` was a dict on line/bar but a repr-string on scatter
  - scatter/bar series omitted the honesty fields curves carried
  - axis titles laundered body text / a neighbor panel's title into an
    authoritative field
  - `y_axis_right` was produced by the module but dropped by the server
  - the tool return shape had to be uniform (always a list)

The benchmarks are producer-side (are the numbers right?). This file is
consumer-side (is the response a clean, uniform, honest contract?). It walks
the tool across every status and asserts the invariants a caller relies on,
so a future schema drift can't slip past a green "0 wrong-emit" run.
"""

from pathlib import Path

import pytest

from pdf_mcp import server

SYN = (
    Path(__file__).parent.parent / "benchmark_data" / "chart_extraction" / "syn_corpus"
)
REAL = Path(__file__).parent.parent / "benchmark_data" / ".reading_order_pdfs"

# (fixture, page, expected status) — one representative per status/kind path.
CASES = [
    ("line_color_linear", 1, "ok"),  # single line, ok
    ("bar_simple", 1, "ok"),  # bar kind
    ("scatter_simple", 1, "ok"),  # scatter kind
    ("line_dual_axis", 1, "needs_hint"),  # dual-axis question
    ("decoy_diagram", 1, "declined"),  # declined + render
]

_SERIES_REQUIRED = {"kind", "style", "label", "resolved_by", "axis"}


@pytest.fixture(autouse=True)
def _clean_cache(isolated_server):
    """CRITICAL: run every contract test against a FRESH cache. Without this
    the tool serves stale page_charts rows and the tests validate the OLD
    response shape — the exact cache-masking that hid these bugs during
    development (a probe key added to the live response passed the schema
    guard until the cache was isolated)."""
    return isolated_server


def _call(name, page, **kw):
    return server.pdf_extract_chart(path=str(SYN / f"{name}.pdf"), page=page, **kw)


def _assert_axis(ax):
    assert set(ax) >= {"scale", "r2", "title", "range"}, ax
    assert ax["scale"] in ("linear", "log")
    title = ax["title"]
    # a title is a short label or null — never a sentence/caption (the
    # pollution class). This single assertion would have caught both the
    # body-text and cross-panel-theft bugs.
    assert title is None or (isinstance(title, str) and len(title) <= 45), title
    assert title is None or not title.lower().startswith(("figure", "fig", "table"))
    assert ax["range"] is None or (
        len(ax["range"]) == 2 and all(isinstance(v, float) for v in ax["range"])
    )


@pytest.mark.parametrize("name,page,expected", CASES)
def test_response_contract(name, page, expected):
    r = _call(name, page)

    # ENVELOPE: always a list, result[0] is the response object.
    assert isinstance(r, list) and len(r) >= 1, "tool must always return a list"
    body = r[0]
    assert isinstance(body, dict)
    if "error" in body:
        pytest.fail(f"unexpected error: {body['error']}")
    assert body["status"] == expected
    assert isinstance(body.get("from_cache"), bool)

    for ch in body.get("charts", []):
        assert set(ch) >= {
            "chart_id",
            "chart_type",
            "x_axis",
            "y_axis",
            "series",
            "diagnostics",
            "render_path",
        }, ch
        _assert_axis(ch["x_axis"])
        _assert_axis(ch["y_axis"])
        if "y_axis_right" in ch:
            _assert_axis(ch["y_axis_right"])
            assert ch["y_axis_right"]["side"] == "right"
        # diagnostics.notes is always present (uniform across kinds)
        assert isinstance(ch["diagnostics"].get("notes"), list)
        # SERIES uniformity — same field set regardless of kind, style is
        # always the structured dict (never a repr string).
        for s in ch["series"]:
            assert _SERIES_REQUIRED <= set(
                s
            ), f"{ch['chart_id']} series missing keys: {s}"
            assert isinstance(
                s["style"], dict
            ), f"style must be a dict, got {s['style']!r}"
            assert set(s["style"]) >= {"color", "width"}
            assert s["style"]["color"] is None or len(s["style"]["color"]) == 3
            assert s["kind"] in ("curve", "bars", "points")


def test_declined_and_hint_carry_inline_image():
    # declined: full-page render inline (the fallback must be viewable)
    d = _call("decoy_diagram", 1)
    assert d[0]["status"] == "declined"
    assert any(
        getattr(b, "type", None) == "image" for b in d[1:]
    ), "declined needs image"
    # needs_hint: annotated render inline
    h = _call("line_dual_axis", 1)
    assert h[0]["status"] == "needs_hint"
    assert any(getattr(b, "type", None) == "image" for b in h[1:]), "hint needs image"
    # ok: NO image on the hot path unless asked
    ok = _call("line_color_linear", 1)
    assert not any(getattr(b, "type", None) == "image" for b in ok[1:])
    ok_r = _call("line_color_linear", 1, include_render=True)
    assert any(getattr(b, "type", None) == "image" for b in ok_r[1:])


def test_error_envelope_is_still_a_list():
    r = server.pdf_extract_chart(path="/does/not/exist.pdf", page=1)
    assert isinstance(r, list) and len(r) == 1 and "error" in r[0]


# --- schema<->version coupling guard ---------------------------------------
# The CHART_EXTRACTION_VERSION bump was forgotten TWICE, and stale page_charts
# cache then served the old shape. A comment-rule wasn't enough. This pins the
# emitted chart-object key set to the version: change the shape and this test
# fails, and the fix (edit EXPECTED_CHART_KEYS) sits right next to the version
# you must bump — so the two move together instead of drifting silently.
EXPECTED_CHART_KEYS = {
    "chart_id",
    "chart_type",
    "region_bbox",
    "x_axis",
    "y_axis",
    "series",
    "diagnostics",
    "render_path",
}  # optional, status-dependent keys (y_axis_right, decline_reason) excluded
EXPECTED_SCHEMA_VERSION = 4  # BUMP THIS whenever the set above changes


def test_response_schema_coupled_to_version():
    from pdf_mcp.chart_extractor import CHART_EXTRACTION_VERSION

    r = _call("line_color_linear", 1)
    keys = set(r[0]["charts"][0])
    assert keys == EXPECTED_CHART_KEYS, (
        f"chart response schema changed ({keys ^ EXPECTED_CHART_KEYS}). "
        "If intended: update EXPECTED_CHART_KEYS AND bump both "
        "EXPECTED_SCHEMA_VERSION and CHART_EXTRACTION_VERSION (cached rows "
        "carry the version; a change without a bump serves the old shape)."
    )
    assert CHART_EXTRACTION_VERSION == EXPECTED_SCHEMA_VERSION, (
        "CHART_EXTRACTION_VERSION and EXPECTED_SCHEMA_VERSION disagree — bump "
        "them together whenever the response shape changes."
    )


@pytest.mark.skipif(
    not (REAL / "1807.11632.pdf").exists(), reason="real corpus not fetched"
)
def test_dual_axis_real_chart_exposes_both_axes():
    r = server.pdf_extract_chart(path=str(REAL / "1807.11632.pdf"), page=4)
    ch = r[0]["charts"][0]
    assert "y_axis_right" in ch, "dual-axis chart must expose the right axis"
    assert {s["axis"] for s in ch["series"]} == {"left", "right"}
