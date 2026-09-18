"""In-process per-document embedding matrix cache: parity with the per-blob
scoring path, LRU eviction by bytes, and the env-controlled budget."""

import numpy as np
import pytest


def _blobs(pages: dict[int, int], dim: int = 8, seed: int = 0):
    rng = np.random.default_rng(seed)
    out = {}
    for p, n in pages.items():
        vs = rng.normal(size=(n, dim)).astype(np.float32)
        vs /= np.linalg.norm(vs, axis=1, keepdims=True)
        out[p] = [v.tobytes() for v in vs]
    return out


class TestBuildAndScore:
    def test_page_max_matches_per_blob_loop(self):
        from pdf_mcp.vector_cache import build_doc_matrix, score_doc

        blobs = _blobs({0: 3, 1: 1, 3: 5})
        q = np.random.default_rng(1).normal(size=8).astype(np.float32)
        dm = build_doc_matrix(blobs)
        page_max, best = score_doc(dm, q)
        assert dm.pages == [0, 1, 3]
        for i, p in enumerate(dm.pages):
            sims = [float(np.frombuffer(b, dtype=np.float32) @ q) for b in blobs[p]]
            assert page_max[i] == pytest.approx(max(sims), abs=1e-6)
            assert best[i] == int(np.argmax(sims))

    def test_empty_pages_are_skipped_and_all_empty_is_none(self):
        from pdf_mcp.vector_cache import build_doc_matrix

        assert build_doc_matrix({0: [], 2: []}) is None
        assert build_doc_matrix({}) is None
        dm = build_doc_matrix({0: [], 1: _blobs({1: 2})[1]})
        assert dm.pages == [1] and dm.M.shape == (2, 8)

    def test_matrix_is_contiguous_float32(self):
        from pdf_mcp.vector_cache import build_doc_matrix

        dm = build_doc_matrix(_blobs({0: 2}))
        assert dm.M.dtype == np.float32 and dm.M.flags["C_CONTIGUOUS"]
        assert dm.nbytes == dm.M.nbytes


class TestMatrixCache:
    def test_hit_after_miss_and_loader_called_once(self):
        from pdf_mcp.vector_cache import MatrixCache, build_doc_matrix

        c = MatrixCache(max_bytes=10_000_000)
        calls = []

        def loader():
            calls.append(1)
            return build_doc_matrix(_blobs({0: 2}))

        a = c.get(("p", 1.0, "m", 14), loader)
        b = c.get(("p", 1.0, "m", 14), loader)
        assert a is b and len(calls) == 1
        assert c.stats()["hits"] == 1 and c.stats()["misses"] == 1

    def test_evicts_least_recently_used_by_bytes(self):
        from pdf_mcp.vector_cache import MatrixCache, build_doc_matrix

        one = build_doc_matrix(_blobs({0: 10}))  # 10 * 8 * 4 = 320 bytes
        c = MatrixCache(max_bytes=one.nbytes * 2 + 1)
        c.get(("a",), lambda: one)
        c.get(("b",), lambda: build_doc_matrix(_blobs({0: 10}, seed=2)))
        c.get(("a",), lambda: one)  # touch a, so b is least recently used
        c.get(("c",), lambda: build_doc_matrix(_blobs({0: 10}, seed=3)))
        assert c.stats()["entries"] == 2

        def reloaded():
            raise AssertionError("a was evicted")

        assert c.get(("a",), reloaded) is one

    def test_zero_budget_never_stores(self):
        from pdf_mcp.vector_cache import MatrixCache, build_doc_matrix

        c = MatrixCache(max_bytes=0)
        n = []

        def loader():
            n.append(1)
            return build_doc_matrix(_blobs({0: 1}))

        c.get(("a",), loader)
        c.get(("a",), loader)
        assert len(n) == 2 and c.stats()["entries"] == 0

    def test_loader_returning_none_is_not_cached(self):
        from pdf_mcp.vector_cache import MatrixCache

        c = MatrixCache(max_bytes=1000)
        assert c.get(("x",), lambda: None) is None
        assert c.stats()["entries"] == 0

    def test_env_budget_in_megabytes(self, monkeypatch):
        from pdf_mcp import vector_cache

        monkeypatch.setenv("PDF_MCP_VECTOR_CACHE_MB", "3")
        assert vector_cache._max_bytes_from_env() == 3 * 1024 * 1024
        monkeypatch.setenv("PDF_MCP_VECTOR_CACHE_MB", "junk")
        assert vector_cache._max_bytes_from_env() == 256 * 1024 * 1024
        monkeypatch.delenv("PDF_MCP_VECTOR_CACHE_MB")
        assert vector_cache._max_bytes_from_env() == 256 * 1024 * 1024


class TestPageMaxHelper:
    def test_page_max_over_decoded_vectors(self):
        from pdf_mcp.vector_cache import page_max_from_lists

        q = np.array([1, 0, 0, 0], dtype=np.float32)
        emb = {
            2: [
                np.array([0, 1, 0, 0], np.float32),
                np.array([0.5, 0.5, 0, 0], np.float32),
            ],
            0: [np.array([1, 0, 0, 0], np.float32)],
            5: [],
        }
        pages, scores = page_max_from_lists(emb, q)
        assert pages == [0, 2]
        assert scores.tolist() == pytest.approx([1.0, 0.5])

    def test_empty_input(self):
        from pdf_mcp.vector_cache import page_max_from_lists

        pages, scores = page_max_from_lists({}, np.zeros(4, np.float32))
        assert pages == [] and len(scores) == 0


class TestCorpusScoresParity:
    def test_matrix_path_equals_blob_path(self, tmp_path, monkeypatch):
        """Same scored tuples and best_chunks as the per-blob reference."""
        import pdf_mcp.server as srv
        from pdf_mcp.extractor import page_embedding_units
        from pdf_mcp.vector_cache import CACHE

        text = ". ".join(f"sentence number {i} about topic {i % 7}" for i in range(400))
        units = page_embedding_units(text)
        assert len(units) > 2
        rng = np.random.default_rng(0)
        vs = rng.normal(size=(len(units), 8)).astype(np.float32)
        vs /= np.linalg.norm(vs, axis=1, keepdims=True)
        blobs = {0: [v.tobytes() for v in vs], 1: [vs[0].tobytes()]}

        class FakeCache:
            cache_dir = tmp_path

            def get_metadata(self, path):
                return {"page_count": 2}

            def get_page_embeddings(self, path, pages, model):
                return blobs

            def get_page_text(self, path, p):
                return text if p == 0 else "short"

        monkeypatch.setattr(srv, "cache", FakeCache())
        CACHE.clear()
        q = rng.normal(size=8).astype(np.float32)
        best: dict = {}
        scored, unproc = srv._corpus_semantic_scores(
            [str(tmp_path / "x.pdf")], "m", q, best_chunks=best
        )
        ref = {
            p: max(float(np.frombuffer(b, dtype=np.float32) @ q) for b in bl)
            for p, bl in blobs.items()
        }
        assert unproc == []
        assert {(p, round(s, 6)) for _, p, s in scored} == {
            (p + 1, round(s, 6)) for p, s in ref.items()
        }
        sub = [float(v @ q) for v in vs[1:]]
        key = (str(tmp_path / "x.pdf"), 1)
        assert best[key] == units[1 + int(np.argmax(sub))]
        assert (str(tmp_path / "x.pdf"), 2) not in best
        # second call is served from the matrix cache
        before = CACHE.stats()["hits"]
        srv._corpus_semantic_scores([str(tmp_path / "x.pdf")], "m", q)
        assert CACHE.stats()["hits"] == before + 1

    def test_doc_without_embeddings_is_unprocessed(self, tmp_path, monkeypatch):
        import pdf_mcp.server as srv
        from pdf_mcp.vector_cache import CACHE

        class FakeCache:
            def get_metadata(self, path):
                return {"page_count": 3}

            def get_page_embeddings(self, path, pages, model):
                return {}

        monkeypatch.setattr(srv, "cache", FakeCache())
        CACHE.clear()
        scored, unproc = srv._corpus_semantic_scores(
            ["/nope.pdf"], "m", np.zeros(8, np.float32)
        )
        assert scored == [] and unproc == ["/nope.pdf"]


class TestLazyBestChunks:
    def test_scorer_records_index_without_reading_page_text(
        self, tmp_path, monkeypatch
    ):
        """Best-window text is resolved only when a returned page asks for
        it: the scorer must not read or re-chunk every page per query."""
        import pdf_mcp.server as srv
        from pdf_mcp.extractor import page_embedding_units
        from pdf_mcp.vector_cache import CACHE

        text = ". ".join(f"sentence number {i} about topic {i % 7}" for i in range(400))
        units = page_embedding_units(text)
        rng = np.random.default_rng(0)
        vs = rng.normal(size=(len(units), 8)).astype(np.float32)
        vs /= np.linalg.norm(vs, axis=1, keepdims=True)
        blobs = {0: [v.tobytes() for v in vs], 1: [vs[0].tobytes()]}
        reads = []

        class FakeCache:
            cache_dir = tmp_path

            def get_metadata(self, path):
                return {"page_count": 2}

            def get_page_embeddings(self, path, pages, model):
                return blobs

            def get_page_text(self, path, p):
                reads.append(p)
                return text if p == 0 else "short"

        monkeypatch.setattr(srv, "cache", FakeCache())
        CACHE.clear()
        q = rng.normal(size=8).astype(np.float32)
        lazy = srv._LazyBestChunks()
        srv._corpus_semantic_scores([str(tmp_path / "x.pdf")], "m", q, best_chunks=lazy)
        assert reads == []  # nothing read during scoring
        assert bool(lazy) and (str(tmp_path / "x.pdf"), 1) in lazy
        assert lazy.get((str(tmp_path / "x.pdf"), 2)) is None  # single-unit page
        sub = [float(v @ q) for v in vs[1:]]
        assert lazy.get((str(tmp_path / "x.pdf"), 1)) == units[1 + int(np.argmax(sub))]
        assert reads == [0]  # one read, on demand
        lazy.get((str(tmp_path / "x.pdf"), 1))
        assert reads == [0]  # cached after the first resolution
