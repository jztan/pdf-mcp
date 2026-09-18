# tests/test_cache_german_fts.py
"""Tests for the optional German FTS mirror ([fts] language = "de").

Structural counterpart to the CJK tests in test_cache.py: same contract
(index-time transform must equal query-time transform), different
transform (Snowball stemming instead of char-splitting).
"""

import pytest

from pdf_mcp.cache import PDFCache, _fts5_or_fallback_de, _german_normalize
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


def test_german_normalize_keeps_digits_as_their_own_token():
    # Regression: an earlier _GERMAN_TOKEN_RE dropped digits entirely (as
    # non-word characters), so "§ 626 BGB" normalized down to just "bgb"
    # and a query for "626" matched nothing while matching every page that
    # merely mentions "BGB". Digits must survive as tokens, unstemmed (the
    # Snowball German stemmer is the identity on digit-only input).
    assert _german_normalize("§ 626 BGB") == "626 bgb"
    assert _german_normalize("626") == "626"
    assert _german_normalize("2023") == "2023"


def test_or_fallback_de_joins_stems_with_or():
    # "§ 622 BGB" is 3 raw tokens (qualifies via _fts5_or_fallback's rule)
    # but only 2 German stems ("§" is dropped as a separator) -- built from
    # stems, not raw tokens, so it stays consistent with the index-time
    # transform.
    assert _fts5_or_fallback_de("§ 622 BGB") == '"622" OR "bgb"'


def test_or_fallback_de_is_none_for_a_two_stem_query():
    # "befristeter Arbeitsvertrag" is 2 raw tokens, so it fails
    # _fts5_or_fallback's own qualification rule regardless of stem count.
    assert _fts5_or_fallback_de("befristeter Arbeitsvertrag") is None


def test_or_fallback_de_is_none_when_raw_tokens_qualify_but_stems_dont():
    # 3 raw tokens qualify under _fts5_or_fallback ("§" counts as its own
    # token there), but German normalization drops "§" as a separator,
    # leaving a single stem -- the OR form would be identical to the AND
    # form, so there's nothing to retry.
    assert _fts5_or_fallback_de("§ 626 §") is None


def test_or_fallback_de_is_none_when_no_tokens_survive():
    assert _fts5_or_fallback_de("   ***   ") is None


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

    def test_get_fts_page_counts_does_not_restem_page_text(
        self, de_cache, tmp_path, monkeypatch
    ):
        # Regression: counting re-stemmed the raw text of every matched page
        # on every query. The OR fallback makes a long German question match
        # nearly every page, so one query on the 490-page BGB stemmed ~234k
        # words and took 4-5 s. Counts must come from the already-stemmed
        # mirror text, so stemming work scales with the query, not the pages.
        path = _touch_pdf(tmp_path, "long.pdf")
        page = "Die Kündigung des Mietvertrags durch den Vermieter. " * 200
        de_cache.save_pages_text(path, {i: page for i in range(20)})

        import pdf_mcp.cache as cache_mod

        real = cache_mod._get_german_stemmer()
        stemmed = []

        class _Counting:
            def stemWord(self, word):
                stemmed.append(word)
                return real.stemWord(word)

            def stemWords(self, words):
                stemmed.extend(words)
                return real.stemWords(words)

        monkeypatch.setattr(cache_mod, "_get_german_stemmer", lambda: _Counting())
        # 4 words, and "Pacht" is on no page: the AND form misses, so the
        # OR fallback matches all 20 pages.
        counts = de_cache.get_fts_page_counts(path, "Kündigung Mietvertrag Pacht Frist")

        assert counts == {i: 400 for i in range(20)}
        assert len(stemmed) < 50

    def test_numeric_query_matches_a_statute_citation_not_bare_bgb(
        self, de_cache, tmp_path
    ):
        # Regression for the digit-dropping tokenizer bug: "626" must find
        # the page citing "§ 626 BGB" and must NOT also return an unrelated
        # page that only mentions "BGB" without that number.
        cite_path = _touch_pdf(tmp_path, "de_cite.pdf")
        de_cache.save_page_text(cite_path, 0, "Die fristlose Kündigung nach § 626 BGB.")
        other_path = _touch_pdf(tmp_path, "de_other.pdf")
        de_cache.save_page_text(other_path, 0, "Das BGB regelt viele Alltagsfragen.")

        assert [r["page"] for r in de_cache.search_fts(cite_path, "626", 10, 100)] == [
            1
        ]
        assert de_cache.search_fts(other_path, "626", 10, 100) == []

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


class TestGermanMirrorCompleteness:
    """The mirror is only useful to search if it stays complete no matter
    which PDFCache instance last wrote a document -- see the PR #44 review:
    once search reads from pdf_search_fts_de, every writer has to maintain
    it, and warm_cli / a corpus warmed offline is exactly the process that
    otherwise writes page_text without ever touching the mirror."""

    def test_plain_cache_writer_keeps_the_mirror_complete(self, tmp_path):
        # 1. A cache with [fts] language = "de" creates the mirror tables.
        PDFCache(cache_dir=tmp_path, ttl_hours=1, fts_language="de")

        # 2. A DIFFERENT, plain cache (no fts_language -- e.g. pdf-mcp-warm
        # run under a config that hadn't set [fts] language yet) writes a
        # new document into the SAME cache_dir.
        plain = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        assert plain._de_tables_exist is True  # sees the mirror already exists
        path = _touch_pdf(tmp_path, "warmed_plain.pdf")
        plain.save_pages_text(
            path, {0: "Die Kündigung war wirksam.", 1: "Urlaubsanspruch"}
        )

        # 3. Re-opening with fts_language="de" must see it immediately --
        # both because `plain` maintained the mirror directly (item 4) and
        # because the open-time sync is a safety net either way.
        de_cache = PDFCache(cache_dir=tmp_path, ttl_hours=1, fts_language="de")
        results = de_cache.search_fts(path, "kündigen", 10, 100)
        assert [r["page"] for r in results] == [1]
        assert de_cache.get_fts_page_counts(path, "kündigen") == {0: 1}

    def test_open_time_sync_backfills_a_document_written_before_de_mode(self, tmp_path):
        # A document fully warmed BEFORE the mirror tables even exist.
        plain = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        path = _touch_pdf(tmp_path, "warmed_before_de.pdf")
        plain.save_page_text(path, 0, "Die Kündigung des Vertrags war wirksam.")

        # First "de" open must backfill it (not just documents that come
        # in after).
        de_cache = PDFCache(cache_dir=tmp_path, ttl_hours=1, fts_language="de")
        assert [r["page"] for r in de_cache.search_fts(path, "kündigen", 10, 100)] == [
            1
        ]

        # A second "de" open must not duplicate the now-synced rows.
        de_cache2 = PDFCache(cache_dir=tmp_path, ttl_hours=1, fts_language="de")
        assert de_cache2.get_fts_page_counts(path, "kündigen") == {0: 1}

    def test_stale_mirror_rows_are_dropped_on_reinvalidate(self, tmp_path):
        de_cache = PDFCache(cache_dir=tmp_path, ttl_hours=1, fts_language="de")
        path = _touch_pdf(tmp_path, "shrinks.pdf")
        de_cache.save_pages_text(
            path,
            {0: "Die Kündigung war wirksam.", 1: "Kündigung erneut erwähnt."},
        )
        assert de_cache.get_fts_page_counts(path, "kündigen") == {0: 1, 1: 1}

        de_cache._invalidate_file(path)
        de_cache.save_page_text(path, 0, "Urlaubsanspruch ohne Bezug.")

        # Page 1's mirror row must be gone, not just shadowed -- confirmed
        # via a fresh open, which runs the count-based sync and would
        # otherwise re-insert a stale row.
        de_cache2 = PDFCache(cache_dir=tmp_path, ttl_hours=1, fts_language="de")
        assert de_cache2.get_fts_page_counts(path, "kündigen") == {}

    def test_blank_page_does_not_force_a_re_sync_on_every_open(
        self, tmp_path, monkeypatch
    ):
        """Regression: _sync_de_tables used to filter out empty-text pages
        when re-inserting into the mirror, but the writers (save_page_text
        et al.) insert one mirror row per page_text row REGARDLESS of
        whether the text is empty. A document with even one blank page
        (common: an image-only page ahead of any OCR pass) therefore never
        reached page_count == de_page_count, and was deleted + fully
        re-stemmed on every single "de" cache open, forever."""
        plain = PDFCache(cache_dir=tmp_path, ttl_hours=1)
        path = _touch_pdf(tmp_path, "with_blank_page.pdf")
        plain.save_pages_text(path, {0: "Die Kündigung war wirksam.", 1: ""})

        # First "de" open backfills it, blank page included.
        PDFCache(cache_dir=tmp_path, ttl_hours=1, fts_language="de")

        import pdf_mcp.cache as cache_mod

        original_normalize = cache_mod._german_normalize
        calls: list[str] = []

        def _spy(text: str) -> str:
            calls.append(text)
            return original_normalize(text)

        monkeypatch.setattr(cache_mod, "_german_normalize", _spy)

        # A second "de" open must find the document already in sync and do
        # no re-stemming work at all for it.
        de_cache2 = PDFCache(cache_dir=tmp_path, ttl_hours=1, fts_language="de")
        assert calls == []
        assert [r["page"] for r in de_cache2.search_fts(path, "kündigen", 10, 100)] == [
            1
        ]

    def test_partial_mirror_falls_back_to_stemming_for_that_query(
        self, de_cache, tmp_path
    ):
        """A mirror row can go missing for one page of an otherwise-synced
        document mid-session (e.g. a concurrent writer with a stale
        _de_tables_exist deleting and only partially re-inserting). Both
        search_fts (via _build_temp_page_fts) and get_fts_page_counts must
        detect the count mismatch against page_text and fall back to
        stemming directly, not silently search/count only the remaining
        rows."""
        path = _touch_pdf(tmp_path, "partial_mirror.pdf")
        de_cache.save_pages_text(
            path,
            {0: "Die Kündigung war wirksam.", 1: "Kündigung erneut erwähnt."},
        )
        with de_cache._connect() as conn:
            conn.execute(
                "DELETE FROM pdf_search_fts_de WHERE file_path = ? AND page_num = 1",
                (path,),
            )

        results = de_cache.search_fts(path, "kündigen", 10, 100)
        assert sorted(r["page"] for r in results) == [1, 2]
        assert de_cache.get_fts_page_counts(path, "kündigen") == {0: 1, 1: 1}


class TestGermanOrFallback:
    """PR #44 round 2: 'de' mode must retry an unmatched multi-word query
    with its stems OR-joined, same as the default keyword path -- an agent
    querying "§ 626 BGB" against a page that cites "§ 626" without ever
    saying "BGB" must still get that page back instead of an empty
    result. Relative BM25 ranking of OR-recovered pages against each other
    is exercised end-to-end by scripts/benchmark_german_fts.py's
    "§ 622 BGB" case, not asserted here."""

    def test_and_only_query_falls_back_to_or(self, de_cache, tmp_path):
        cite_path = _touch_pdf(tmp_path, "cite.pdf")
        de_cache.save_pages_text(
            cite_path,
            {
                # Cites the number without ever saying "BGB".
                0: "Die fristlose Kündigung nach § 626.",
                # Says "BGB" without this number -- an OR match, but not
                # the citation the query is actually after.
                1: "Das BGB regelt Alltagsfragen.",
            },
        )
        results = de_cache.search_fts(cite_path, "§ 626 BGB", 10, 100)
        assert sorted(r["page"] for r in results) == [1, 2]

        counts = de_cache.get_fts_page_counts(cite_path, "§ 626 BGB")
        assert counts == {0: 1, 1: 1}

    def test_multi_document_comparison_path_still_gets_no_fallback(
        self, de_cache, tmp_path
    ):
        # allow_or_fallback=False is how server.py's multi-document
        # comparison calls search_fts -- relaxing every document
        # independently there would flood the comparison with loose
        # single-term hits (same reasoning as the default keyword path).
        path = _touch_pdf(tmp_path, "cite2.pdf")
        de_cache.save_page_text(path, 0, "Die fristlose Kündigung nach § 626.")
        assert (
            de_cache.search_fts(path, "§ 626 BGB", 10, 100, allow_or_fallback=False)
            == []
        )

    def test_section_search_also_falls_back_to_or(self, de_cache, tmp_path):
        path = _touch_pdf(tmp_path, "cite_sec.pdf")
        de_cache.index_sections(
            path,
            [
                Section(
                    title="Fristlose Kündigung",
                    start_page=1,
                    end_page=2,
                    text="Die fristlose Kündigung nach § 626.",
                    title_source="heuristic",
                )
            ],
        )
        results = de_cache.search_section_fts(path, "§ 626 BGB", 10)
        assert len(results) == 1
        assert results[0]["title"] == "Fristlose Kündigung"
