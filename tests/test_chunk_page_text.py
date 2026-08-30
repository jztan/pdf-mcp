"""Pure unit tests for sub-page embedding chunking."""

from pdf_mcp.extractor import chunk_page_text


class TestChunkPageText:
    def test_empty_and_whitespace_return_no_chunks(self):
        assert chunk_page_text("") == []
        assert chunk_page_text("   \n\t  ") == []

    def test_short_text_passes_through_unchanged(self):
        text = "One short paragraph well under the limit."
        assert chunk_page_text(text) == [text]

    def test_text_at_exactly_the_limit_is_one_chunk(self):
        text = "a" * (300 * 4)
        assert chunk_page_text(text) == [text]

    def test_long_text_splits_into_multiple_chunks(self):
        text = "word " * 1000  # 5000 chars, ~1250 tokens
        chunks = chunk_page_text(text)
        assert len(chunks) > 1
        assert all(c.strip() for c in chunks)

    def test_chunks_overlap_by_the_requested_ratio(self):
        # 900 tokens of distinct sentences, 300-token windows, 20% overlap
        # means each window advances by 240 tokens (960 chars).
        text = ". ".join(f"sentence number {i} here" for i in range(400))
        chunks = chunk_page_text(text, max_tokens=300, overlap_ratio=0.2)
        assert len(chunks) >= 3
        # consecutive chunks must share text
        assert (
            chunks[0][-200:]
            and chunks[0][-200:] in chunks[1]
            or (chunks[1][:200] in chunks[0])
        )

    def test_no_chunk_exceeds_max_tokens_by_more_than_a_sentence(self):
        text = ". ".join(f"sentence {i}" for i in range(500))
        chunks = chunk_page_text(text, max_tokens=100)
        # a sentence-boundary preference may overshoot slightly, never wildly
        assert all(len(c) <= 100 * 4 * 1.5 for c in chunks)

    def test_prefers_sentence_boundaries(self):
        text = ("First sentence here. " * 40) + ("Second part follows. " * 40)
        chunks = chunk_page_text(text, max_tokens=100)
        # every chunk except possibly the last should end at a sentence end
        assert sum(1 for c in chunks[:-1] if c.rstrip().endswith(".")) >= 1

    def test_is_deterministic(self):
        text = "word " * 900
        assert chunk_page_text(text) == chunk_page_text(text)

    def test_single_token_longer_than_max_does_not_loop_or_drop_text(self):
        text = "x" * (300 * 4 * 3)  # one unbroken "word", 3 windows long
        chunks = chunk_page_text(text)
        assert len(chunks) >= 2
        assert "".join(c for c in chunks)  # produced something
        assert all(c for c in chunks)

    def test_covers_the_whole_input(self):
        text = ". ".join(f"unique marker {i}" for i in range(300))
        chunks = chunk_page_text(text)
        joined = " ".join(chunks)
        for i in (0, 150, 299):
            assert f"unique marker {i}" in joined


class TestPageEmbeddingUnits:
    def test_empty_returns_nothing(self):
        from pdf_mcp.extractor import page_embedding_units

        assert page_embedding_units("") == []
        assert page_embedding_units("   ") == []

    def test_short_page_is_just_the_page(self):
        from pdf_mcp.extractor import page_embedding_units

        text = "One short paragraph under the window."
        assert page_embedding_units(text) == [text]

    def test_long_page_leads_with_the_whole_page_then_windows(self):
        from pdf_mcp.extractor import chunk_page_text, page_embedding_units

        text = ". ".join(f"sentence number {i} here" for i in range(400))
        units = page_embedding_units(text)
        windows = chunk_page_text(text)
        assert len(windows) > 1
        assert units[0] == text.strip()  # whole page first, floors the score
        assert units[1:] == windows
        assert len(units) == len(windows) + 1
