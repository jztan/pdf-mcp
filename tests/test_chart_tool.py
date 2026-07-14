"""Tool-level tests for pdf_extract_chart (wiring, cache, errors)."""

import json
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
    result = call(path=str(SYN / "line_color_linear.pdf"), page=1)
    assert isinstance(result, list)
    r = result[0]
    assert "error" not in r
    assert r["status"] == "ok"
    # "ok" status: no image blocks by default
    assert len(result) == 1
    chart = r["charts"][0]
    assert chart["chart_type"] == "line"
    assert chart["series"][0]["points"]
    assert Path(chart["render_path"]).exists()
    assert r["from_cache"] is False
    r2 = call(path=str(SYN / "line_color_linear.pdf"), page=1)[0]
    assert r2["from_cache"] is True


def test_ok_flow_json_serializable(call):
    result = call(path=str(SYN / "line_color_linear.pdf"), page=1)
    json.dumps(result[0])  # must not raise


def test_ok_with_include_render_emits_image_blocks(call):
    result = call(path=str(SYN / "line_color_linear.pdf"), page=1, include_render=True)
    assert len(result) >= 2
    r = result[0]
    assert r["status"] == "ok"
    for block in result[1:]:
        assert block.type == "image"
        assert block.meta["kind"] == "chart_region"
        assert block.meta["page"] == 1


def test_needs_hint_then_hints_roundtrip(call):
    result = call(path=str(SYN / "line_dual_axis.pdf"), page=1)
    r1 = result[0]
    assert r1["status"] == "needs_hint"
    # needs_hint: at least one inline image block (annotated hint render)
    assert len(result) >= 2
    block = result[1]
    assert block.type == "image"
    assert block.meta["kind"] == "hint_panel"
    q = r1["questions"][0]
    assert q["options"] == ["left", "right"]
    assert Path(q["render_path"]).exists()
    hints = {qq["id"]: "left" for qq in r1["questions"]}
    r2 = call(path=str(SYN / "line_dual_axis.pdf"), page=1, hints=hints)[0]
    assert r2["status"] == "ok"


def test_declined_returns_page_render(call):
    result = call(path=str(SYN / "decoy_diagram.pdf"), page=1)
    r = result[0]
    assert r["status"] == "declined"
    assert r["reasons"]
    assert Path(r["render_path"]).exists()
    # declined: exactly one inline image block (the full-page render)
    assert len(result) == 2
    block = result[1]
    assert block.type == "image"
    assert block.meta["kind"] == "declined_page"


def test_declined_chart_reason_surfaced_in_response(call):
    result = call(path=str(SYN / "line_mono_crossing.pdf"), page=1)
    r = result[0]
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


def test_render_unavailable_on_missing_file(call, isolated_server):
    """If the cached render_path file is gone by the time a cache-hit
    re-attaches its image block (e.g. an OS temp-dir sweep, or the renders
    dir was cleared out-of-band), the tool must degrade gracefully: no image
    block for that render, render_unavailable=True, and no exception."""
    result = call(path=str(SYN / "decoy_diagram.pdf"), page=1)
    r = result[0]
    assert r["status"] == "declined"
    render_path = Path(r["render_path"])
    assert render_path.exists()

    # First call populated the cache; delete the render file on disk so the
    # *next* call (a cache hit) fails to re-attach the image.
    render_path.unlink()

    result2 = call(path=str(SYN / "decoy_diagram.pdf"), page=1)
    r2 = result2[0]
    assert r2["from_cache"] is True
    assert r2.get("render_unavailable") is True
    # No image block was produced for the missing render.
    assert len(result2) == 1
    assert all(getattr(b, "type", None) != "image" for b in result2[1:])


def test_render_oversized_on_missing_file(call, isolated_server, monkeypatch):
    """When a render PNG exceeds the transport byte budget, the tool must
    drop the image block, flag render_oversized=True on the response, and
    not raise — rather than silently omitting the flag or crashing."""
    from pdf_mcp import server

    monkeypatch.setattr(server, "RENDER_RESULT_BYTE_BUDGET", 10)

    result = call(path=str(SYN / "decoy_diagram.pdf"), page=1)
    r = result[0]
    assert r["status"] == "declined"
    assert r.get("render_oversized") is True
    assert len(result) == 1
    assert all(getattr(b, "type", None) != "image" for b in result[1:])


def test_inline_errors(call):
    r = call(path="/nonexistent.pdf", page=1)
    assert isinstance(r, list) and len(r) == 1
    assert "error" in r[0]

    r = call(path=str(SYN / "line_color_linear.pdf"), page=99)
    assert len(r) == 1
    assert "error" in r[0]

    r = call(path=str(SYN / "line_dual_axis.pdf"), page=1, hints={"p9.s9.axis": "left"})
    assert len(r) == 1
    assert "error" in r[0]  # unknown hint id

    r = call(
        path=str(SYN / "line_dual_axis.pdf"), page=1, hints={"p0.s0.axis": "sideways"}
    )
    assert len(r) == 1
    assert "error" in r[0]  # invalid enum value


def test_server_exposes_y_axis_right(call):
    """The server response must surface y_axis_right on a dual-axis chart
    (the module produces it; the response whitelist used to drop it)."""
    from pathlib import Path

    real = (
        Path(__file__).parent.parent
        / "benchmark_data"
        / ".reading_order_pdfs"
        / "1807.11632.pdf"
    )
    if not real.exists():
        import pytest

        pytest.skip("real corpus not fetched")
    r = call(path=str(real), page=4)
    chart = r[0]["charts"][0]
    assert "y_axis_right" in chart
    assert chart["y_axis_right"]["side"] == "right"
    assert "RMSE" in (chart["y_axis_right"]["title"] or "")
    assert len(chart["y_axis_right"]["range"]) == 2
