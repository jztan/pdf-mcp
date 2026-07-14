"""Tool-level tests for pdf_extract_chart (wiring, cache, errors)."""

from pathlib import Path

import pytest

SYN = (
    Path(__file__).parent.parent / "benchmark_data" / "chart_extraction" / "syn_corpus"
)


@pytest.fixture()
def call(isolated_server):
    # isolated_server is the shared conftest fixture giving a clean server
    # module + cache.
    from pdf_mcp import server

    def _call(**kw):
        return server.pdf_extract_chart(**kw)

    return _call


@pytest.fixture()
def call_read(isolated_server):
    # Mirrors `call` above but for pdf_read_pages.
    from pdf_mcp import server

    def _call(**kw):
        return server.pdf_read_pages(**kw)

    return _call


def test_ok_flow_emits_table_and_render(call):
    r = call(path=str(SYN / "line_color_linear.pdf"), page=1)
    assert "error" not in r
    assert r["status"] == "ok"
    chart = r["charts"][0]
    assert chart["chart_type"] == "line"
    assert chart["series"][0]["points"]
    assert Path(chart["render_path"]).exists()
    assert r["from_cache"] is False
    r2 = call(path=str(SYN / "line_color_linear.pdf"), page=1)
    assert r2["from_cache"] is True


def test_needs_hint_then_hints_roundtrip(call):
    r1 = call(path=str(SYN / "line_dual_axis.pdf"), page=1)
    assert r1["status"] == "needs_hint"
    q = r1["questions"][0]
    assert q["options"] == ["left", "right"]
    assert Path(q["render_path"]).exists()
    hints = {qq["id"]: "left" for qq in r1["questions"]}
    r2 = call(path=str(SYN / "line_dual_axis.pdf"), page=1, hints=hints)
    assert r2["status"] == "ok"


def test_declined_returns_page_render(call):
    r = call(path=str(SYN / "decoy_diagram.pdf"), page=1)
    assert r["status"] == "declined"
    assert r["reasons"]
    assert Path(r["render_path"]).exists()


def test_declined_chart_reason_surfaced_in_response(call):
    r = call(path=str(SYN / "line_mono_crossing.pdf"), page=1)
    assert r["status"] == "declined"
    declined = [c for c in r.get("charts", []) if c["chart_type"] == "declined"]
    assert declined, "expected the response to include the declined chart"
    chart = declined[0]
    assert chart["decline_reason"]
    assert any("multivalued" in n for n in chart["diagnostics"]["notes"])


def test_detect_charts_flag(call_read):
    r = call_read(
        path=str(SYN / "line_color_linear.pdf"), pages="1", detect_charts=True
    )
    assert r["pages"][0]["charts_detected"] == 1


def test_detect_charts_default_off(call_read):
    r = call_read(path=str(SYN / "line_color_linear.pdf"), pages="1")
    assert "charts_detected" not in r["pages"][0]


def test_inline_errors(call):
    assert "error" in call(path="/nonexistent.pdf", page=1)
    r = call(path=str(SYN / "line_color_linear.pdf"), page=99)
    assert "error" in r
    r = call(path=str(SYN / "line_dual_axis.pdf"), page=1, hints={"p9.s9.axis": "left"})
    assert "error" in r  # unknown hint id
    r = call(
        path=str(SYN / "line_dual_axis.pdf"), page=1, hints={"p0.s0.axis": "sideways"}
    )
    assert "error" in r  # invalid enum value
