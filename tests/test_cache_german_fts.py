# tests/test_cache_german_fts.py
"""Tests for the optional German FTS mirror ([fts] language = "de").

Structural counterpart to the CJK tests in test_cache.py: same contract
(index-time transform must equal query-time transform), different
transform (Snowball stemming instead of char-splitting).
"""

import pytest

from pdf_mcp.cache import PDFCache, _german_normalize
from pdf_mcp.section_detector import Section


def test_german_normalize_unifies_inflections_and_spelling_variants():
    # Different inflections of the same word share a stem.
    assert _german_normalize("Kündigung") == _german_normalize("kündigen")
    # The three common spellings of an umlaut/ß word converge too (verified
    # against the Snowball German stemmer directly -- see cache.py's
    # _german_normalize docstring).
    assert (
        _german_normalize("Kündigung")
        == _german_normalize("Kuendigung")
        == _german_normalize("kundigung")
    )
    assert _german_normalize("Straße") == _german_normalize("Strasse")


def test_german_normalize_is_order_stable_and_drops_punctuation():
    assert _german_normalize("") == ""
    assert _german_normalize("§ 622 BGB!") == _german_normalize("622 BGB")


@pytest.fixture
def de_cache(tmp_path):
    """A cache opened with [fts] language = "de"."""
    return PDFCache(cache_dir=tmp_path, ttl_hours=1, fts_language="de")


def _touch_pdf(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"%PDF-1.4\n")
    return str(p)


class TestGermanPageSearch:
    def test_finds_inflected_form_via_stemming(self, de_cache, tmp_path):
        path = _touch_pdf(tmp_path, "de.pdf")
        de_cache.save_page_text(path, 0, "Die Kündigung des Vertrags war wirksam.")
        results = de_cache.search_fts(path, "kündigen", 10, 100)
        assert [r["page"] for r in results] == [1]
        assert "Kündigung" in results[0]["excerpt"]

    def test_finds_ascii_transliteration_spelling(self, de_cache, tmp_path):
        # A standalone word, not a compound: German compound nouns
        # ("Fußballverein") stem as one token each -- compound splitting is
        # a deliberately deferred follow-up (see the plan), not covered by
        # stemming alone.
        path = _touch_pdf(tmp_path, "de2.pdf")
        de_cache.save_page_text(path, 0, "Der Verein spielt Fußball im Park.")
        results = de_cache.search_fts(path, "Fussball", 10, 100)
        assert [r["page"] for r in results] == [1]

    def test_batch_save_populates_german_mirror(self, de_cache, tmp_path):
        path = _touch_pdf(tmp_path, "de3.pdf")
        de_cache.save_pages_text(
            path, {0: "Die Kündigung war wirksam.", 1: "Urlaubsanspruch"}
        )
        results = de_cache.search_fts(path, "kündigen", 10, 100)
        assert [r["page"] for r in results] == [1]

    def test_get_fts_page_counts_counts_stem_matches(self, de_cache, tmp_path):
        # "kündigt" (3rd person present) stems to "kundigt", NOT to the same
        # stem as "kündigen"/"Kündigung"/"Kündigungen" ("kundig") -- a real
        # gap in the Snowball German algorithm itself, not this integration.
        # Only the two forms sharing "kundig" are expected to count.
        path = _touch_pdf(tmp_path, "de4.pdf")
        de_cache.save_page_text(
            path, 0, "Kündigung. Der Arbeitgeber kündigt. Kündigungen sind selten."
        )
        counts = de_cache.get_fts_page_counts(path, "kündigen")
        assert counts == {0: 2}

    def test_default_cache_has_no_german_stemming(self, cache, tmp_path):
        """Sanity check: without [fts] language = "de", inflected forms are
        NOT unified (porter's English stemmer does nothing useful here) --
        this is the exact gap the option exists to close."""
        path = _touch_pdf(tmp_path, "en.pdf")
        cache.save_page_text(path, 0, "Die Kündigung des Vertrags war wirksam.")
        assert cache.search_fts(path, "kündigen", 10, 100) == []


class TestGermanSectionSearch:
    def test_finds_inflected_section_and_restores_original_title(
        self, de_cache, tmp_path
    ):
        path = _touch_pdf(tmp_path, "de_sec.pdf")
        de_cache.index_sections(
            path,
            [
                Section(
                    title="Kündigungsschutz",
                    start_page=1,
                    end_page=2,
                    text="Regelungen zur Kündigung von Arbeitsverträgen.",
                    title_source="heuristic",
                )
            ],
        )
        results = de_cache.search_section_fts(path, "kündigen", 10)
        assert len(results) == 1
        assert results[0]["title"] == "Kündigungsschutz"
