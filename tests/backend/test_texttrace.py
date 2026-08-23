"""get_texttrace, which backs the content_trust hidden-text detector.

This is a safety module whose error path degrades to "looks clean", so
the tests here check values rather than shapes. In the spike a harness
read dict keys that did not exist, both engines returned None, the
comparison agreed, and it reported 14/14 with every attack fixture
passing as safe.
"""

import importlib.util
from pathlib import Path

import pymupdf
import pytest

from pdf_mcp.backend.texttrace import get_texttrace
from tests.backend.differential import assert_non_empty

# Absolute, not cwd-relative: pytest run from any directory must find these.
_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = _ROOT / "benchmark_data/content_trust_corpus"
_INVISIBLE = str(_CORPUS / "attack_invisible_en.pdf")
_TRANSPARENT = str(_CORPUS / "attack_transparent_en.pdf")
_TINY = str(_CORPUS / "attack_tiny_en.pdf")
_WHITE = str(_CORPUS / "attack_white_en.pdf")
_CLEAN = str(_CORPUS / "clean_plain.pdf")


@pytest.fixture(scope="module", autouse=True)
def _corpus():
    """Build the attack corpus if absent (0.12s for all 14 fixtures).

    The PDFs are generated artifacts and gitignored, so a clean checkout
    has only generate.py. Building rather than skipping is deliberate:
    this module tests a SAFETY detector whose failure mode is reporting
    a hidden-text attack as clean, and a skip on CI would have looked
    exactly like a pass. That is how these tests reached CI green
    locally and red on a clean checkout.
    """
    if (_CORPUS / "attack_invisible_en.pdf").is_file():
        return
    spec = importlib.util.spec_from_file_location(
        "_ct_generate", _CORPUS / "generate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build(str(_CORPUS))


def test_chars_carry_integer_codepoints():
    """content_trust rebuilds span text with chr(c[0]). A character here
    raises TypeError, which _scan_page_geometry swallows into
    pages_errored, so every attack fixture reports clean. That exact bug
    happened in the spike."""
    spans = get_texttrace(_INVISIBLE, 0)
    assert_non_empty(spans, "spans")
    for span in spans:
        assert_non_empty(span["chars"], "chars")
        for char in span["chars"]:
            assert isinstance(char[0], int), f"codepoint must be int, got {char[0]!r}"
            chr(char[0])


def test_span_text_reconstructs():
    spans = get_texttrace(_INVISIBLE, 0)
    text = "".join(chr(c[0]) for s in spans for c in s["chars"])
    assert_non_empty(text.strip(), "reconstructed text")


def test_render_mode_matches_pymupdf():
    """type == 3 is the invisible-text render mode the detector keys on."""
    ref_doc = pymupdf.open(_INVISIBLE)
    ref = ref_doc[0].get_texttrace()
    ref_doc.close()
    got = get_texttrace(_INVISIBLE, 0)

    assert_non_empty(ref, "pymupdf spans")
    assert_non_empty(got, "shim spans")
    assert any(s["type"] == 3 for s in ref), "fixture no longer has invisible text"
    assert any(s["type"] == 3 for s in got), "invisible render mode not detected"


def test_transparent_fill_is_visible_as_opacity():
    spans = get_texttrace(_TRANSPARENT, 0)
    assert_non_empty(spans, "spans")
    assert any(
        s["opacity"] <= 0.05 and s["type"] in (0, 2) for s in spans
    ), "transparent span not detected"


def test_tiny_font_size_is_reported():
    spans = get_texttrace(_TINY, 0)
    assert_non_empty(spans, "spans")
    assert any(s["size"] <= 1.0 for s in spans), "tiny font not detected"


def test_white_fill_colour_is_reported():
    spans = get_texttrace(_WHITE, 0)
    assert_non_empty(spans, "spans")
    assert any(
        all(channel >= 0.95 for channel in s["color"]) for s in spans
    ), "white fill not detected"


def test_clean_page_has_no_hidden_signals():
    """The control. A detector that flags everything is useless, and a
    corpus of attacks alone cannot show that."""
    spans = get_texttrace(_CLEAN, 0)
    assert_non_empty(spans, "spans")
    for span in spans:
        assert span["type"] != 3
        assert span["opacity"] > 0.05
        assert span["size"] > 1.0


def test_bbox_is_in_top_left_page_space():
    """content_trust intersects the span rect with page.rect and treats an
    empty intersection as off-page. A bbox in the wrong coordinate space
    makes ordinary text look off-page."""
    ref_doc = pymupdf.open(_CLEAN)
    page_rect = ref_doc[0].rect
    ref_doc.close()
    spans = get_texttrace(_CLEAN, 0)
    assert_non_empty(spans, "spans")
    for span in spans:
        x0, y0, x1, y1 = span["bbox"]
        assert x1 >= x0 and y1 >= y0
        assert -1 <= x0 <= page_rect.width + 1
        assert -1 <= y0 <= page_rect.height + 1


@pytest.mark.parametrize(
    "name",
    [
        "attack_invisible_en",
        "attack_transparent_en",
        "attack_tiny_en",
        "attack_white_en",
        "attack_offpage_en",
    ],
)
def test_every_attack_fixture_yields_spans(name):
    """Non-emptiness per fixture. A backend returning nothing would make
    every downstream check vacuously pass."""
    spans = get_texttrace(f"{_CORPUS}/{name}.pdf", 0)
    assert_non_empty(spans, f"{name} spans")
    assert any(s["chars"] for s in spans), f"{name}: no chars in any span"


class _Page:
    """Minimal page for _scan_page_geometry, which reads only these four.

    rect is a real pymupdf.Rect so the only variable under test is the
    backend data. Image bboxes are real: without them a clean OCR layer
    is reported as an attack, because OCR text is genuinely invisible
    (render mode 3) and is exempted only by sitting over its own scan.
    get_drawings returns [] here; _page_fills is best-effort and only
    refines the white-on-white check, which these fixtures do not need.
    """

    def __init__(self, path, page_num, rect):
        self._path = path
        self._page_num = page_num
        self.rect = rect

    def get_texttrace(self):
        return get_texttrace(self._path, self._page_num)

    def get_image_info(self):
        from pdf_mcp.backend.raster import get_image_info

        return get_image_info(self._path, self._page_num)

    def get_drawings(self):
        return []


ATTACKS = [
    "attack_invisible_en",
    "attack_invisible_cjk",
    "attack_transparent_en",
    "attack_transparent_cjk",
    "attack_tiny_en",
    "attack_tiny_cjk",
    "attack_white_en",
    "attack_white_cjk",
    "attack_offpage_en",
    "attack_offpage_cjk",
]
CONTROLS = [
    "clean_plain",
    "clean_ocr_layer",
    "clean_stray_glyph",
    "clean_prose_about_injection",
]


@pytest.mark.parametrize("name", ATTACKS)
def test_detector_flags_every_attack(name):
    """The consumer-level check. The spike's 14/14 was vacuous: it read
    keys that did not exist, so both engines returned None and every
    attack passed as safe."""
    from pdf_mcp.content_trust import _scan_page_geometry

    path = f"{_CORPUS}/{name}.pdf"
    ref_doc = pymupdf.open(path)
    rect = ref_doc[0].rect
    ref_doc.close()

    spans = _scan_page_geometry(_Page(path, 0, rect), 0)
    assert_non_empty(spans, f"{name}: detector found no hidden span")
    assert all(s["reasons"] for s in spans)


@pytest.mark.parametrize("name", CONTROLS)
def test_detector_leaves_controls_clean(name):
    """A detector that flags everything catches every attack and is
    useless. Both halves have to hold."""
    from pdf_mcp.content_trust import _scan_page_geometry

    path = f"{_CORPUS}/{name}.pdf"
    ref_doc = pymupdf.open(path)
    rect = ref_doc[0].rect
    ref_doc.close()

    spans = _scan_page_geometry(_Page(path, 0, rect), 0)
    assert spans == [], f"{name} flagged: {[s['reasons'] for s in spans]}"
