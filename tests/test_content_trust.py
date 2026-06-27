import pymupdf

from pdf_mcp.content_trust import _scan_page_geometry


def _page(build):
    doc = pymupdf.open()
    page = doc.new_page()
    build(page)
    return doc, page


def _reasons(spans):
    out = set()
    for s in spans:
        out.update(s["reasons"])
    return out


def test_clean_page_has_no_hidden_spans():
    doc, page = _page(
        lambda p: p.insert_text((72, 72), "normal visible body text", fontsize=12)
    )
    assert _scan_page_geometry(page, 0) == []
    doc.close()


def test_tiny_font_detected():
    doc, page = _page(
        lambda p: p.insert_text((72, 72), "tiny hidden secret text here", fontsize=0.5)
    )
    spans = _scan_page_geometry(page, 0)
    assert "tiny_font" in _reasons(spans)
    doc.close()


def test_white_on_white_detected():
    doc, page = _page(
        lambda p: p.insert_text(
            (72, 72), "white hidden secret text", fontsize=12, color=(1, 1, 1)
        )
    )
    assert "white_on_white" in _reasons(_scan_page_geometry(page, 0))
    doc.close()


def test_offpage_detected():
    doc, page = _page(
        lambda p: p.insert_text((72, 2000), "off the page secret text", fontsize=12)
    )
    assert "offpage" in _reasons(_scan_page_geometry(page, 0))
    doc.close()


def _write_invisible(page, text, render_mode=3, opacity=1.0):
    tw = pymupdf.TextWriter(page.rect)
    tw.append((72, 100), text)
    tw.write_text(page, render_mode=render_mode, opacity=opacity)


def test_transparent_detected():
    doc = pymupdf.open()
    page = doc.new_page()
    _write_invisible(page, "transparent secret text", render_mode=0, opacity=0.0)
    assert "transparent" in _reasons(_scan_page_geometry(page, 0))
    doc.close()


def test_invisible_render_detected_when_not_image_backed():
    doc = pymupdf.open()
    page = doc.new_page()
    _write_invisible(page, "invisible injected instructions here", render_mode=3)
    assert "invisible_render" in _reasons(_scan_page_geometry(page, 0))
    doc.close()


def test_ocr_layer_is_exempt():
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200))
    pix.clear_with(200)
    page.insert_image(page.rect, pixmap=pix)
    _write_invisible(page, "ocr layer text over image", render_mode=3)
    assert _scan_page_geometry(page, 0) == []
    doc.close()


def test_min_char_floor_skips_short_invisible_spans():
    doc = pymupdf.open()
    page = doc.new_page()
    _write_invisible(page, "hi", render_mode=3)  # below _MIN_HIDDEN_CHARS
    assert _scan_page_geometry(page, 0) == []
    doc.close()


def test_stroke_only_text_is_visible_not_flagged():
    doc = pymupdf.open()
    page = doc.new_page()
    _write_invisible(
        page, "outlined display heading", render_mode=1
    )  # stroke = visible
    assert _scan_page_geometry(page, 0) == []
    doc.close()


def test_hidden_span_records_text_and_char_count():
    doc, page = _page(
        lambda p: p.insert_text(
            (72, 72), "white hidden secret text", fontsize=12, color=(1, 1, 1)
        )
    )
    spans = _scan_page_geometry(page, 0)
    assert spans and spans[0]["char_count"] >= 8
    assert "secret" in spans[0]["text"]
    assert spans[0]["page"] == 0
    doc.close()
