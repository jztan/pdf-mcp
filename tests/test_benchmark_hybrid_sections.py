import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


def test_mrr_perfect_rank():
    from benchmark_hybrid_sections import mrr

    assert mrr(ranked=["a", "c", "x"], gold={"a", "c"}) == 1.0


def test_mrr_first_gold_at_rank_2():
    from benchmark_hybrid_sections import mrr

    assert mrr(ranked=["x", "a", "c"], gold={"a", "c"}) == 0.5


def test_mrr_no_gold_in_results():
    from benchmark_hybrid_sections import mrr

    assert mrr(ranked=["x", "y", "z"], gold={"a"}) == 0.0


def test_mrr_empty_results():
    from benchmark_hybrid_sections import mrr

    assert mrr(ranked=[], gold={"a"}) == 0.0


def test_recall_at_k_full():
    from benchmark_hybrid_sections import recall_at_k

    assert recall_at_k(ranked=["a", "b", "c"], gold={"a", "b"}, k=5) == 1.0


def test_recall_at_k_partial():
    from benchmark_hybrid_sections import recall_at_k

    assert abs(recall_at_k(["a", "b", "x"], {"a", "b", "c"}, k=2) - 2 / 3) < 1e-9


def test_recall_at_k_truncates_below_k():
    from benchmark_hybrid_sections import recall_at_k

    assert recall_at_k(["x", "y", "z", "a"], {"a"}, k=3) == 0.0


def test_recall_at_k_empty_gold_raises():
    from benchmark_hybrid_sections import recall_at_k
    import pytest

    with pytest.raises(ValueError):
        recall_at_k(["a"], set(), k=5)


def test_recall_at_k_no_gold_in_ranked():
    from benchmark_hybrid_sections import recall_at_k

    assert recall_at_k(["x", "y", "z"], {"a", "b"}, k=10) == 0.0


def test_query_loader_basic(tmp_path):
    from benchmark_hybrid_sections import load_queries
    import json

    f = tmp_path / "q.json"
    f.write_text(
        json.dumps(
            {
                "pdfs": {
                    "x": {
                        "url": "https://example.com/x.pdf",
                        "queries": [
                            {
                                "id": "x_lex_01",
                                "category": "lexical",
                                "query": "Methods",
                                "gold_section_keys": ["S001:p3:Methods"],
                            }
                        ],
                    }
                }
            }
        )
    )
    out = load_queries(str(f))
    assert "x" in out
    assert out["x"]["url"] == "https://example.com/x.pdf"
    assert len(out["x"]["queries"]) == 1
    assert out["x"]["queries"][0]["id"] == "x_lex_01"


def test_query_loader_rejects_unknown_category(tmp_path):
    from benchmark_hybrid_sections import load_queries
    import json
    import pytest

    f = tmp_path / "q.json"
    f.write_text(
        json.dumps(
            {
                "pdfs": {
                    "x": {
                        "url": "u",
                        "queries": [
                            {
                                "id": "1",
                                "category": "weird",
                                "query": "q",
                                "gold_section_keys": ["S000:p1:T"],
                            }
                        ],
                    }
                }
            }
        )
    )
    with pytest.raises(ValueError, match="weird"):
        load_queries(str(f))


def test_query_loader_rejects_missing_field(tmp_path):
    from benchmark_hybrid_sections import load_queries
    import json
    import pytest

    f = tmp_path / "q.json"
    f.write_text(
        json.dumps(
            {
                "pdfs": {
                    "x": {
                        "url": "u",
                        "queries": [{"id": "1", "category": "lexical", "query": "q"}],
                    }
                }
            }
        )
    )
    with pytest.raises(ValueError, match="gold_section_keys"):
        load_queries(str(f))


def test_embed_sections_lazy_skips_already_cached(tmp_path):
    """First call embeds; second call short-circuits when cache hits."""
    from pdf_mcp.cache import PDFCache
    from benchmark_hybrid_sections import embed_sections_for_pdf
    import numpy as np

    pdf_path = tmp_path / "x.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    cache = PDFCache(cache_dir=tmp_path)
    sections = [
        {"id": 0, "key": "S000:p1:A", "text": "alpha"},
        {"id": 1, "key": "S001:p2:B", "text": "beta"},
    ]

    class FakeEmbedder:
        def __init__(self):
            self.calls: list[list[str]] = []

        def embed(self, texts):
            self.calls.append(list(texts))
            return [np.ones(384, dtype="float32") for _ in texts]

    e1 = FakeEmbedder()
    embed_sections_for_pdf(cache, str(pdf_path), sections, e1, model_name="fake")
    assert len(e1.calls) == 1 and len(e1.calls[0]) == 2

    # Second pass: cache should serve everything; new embedder gets zero calls.
    e2 = FakeEmbedder()
    embed_sections_for_pdf(cache, str(pdf_path), sections, e2, model_name="fake")
    assert e2.calls == []


def test_embed_sections_partial_cache_only_embeds_missing(tmp_path):
    from pdf_mcp.cache import PDFCache
    from benchmark_hybrid_sections import embed_sections_for_pdf
    import numpy as np

    pdf_path = tmp_path / "x.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    cache = PDFCache(cache_dir=tmp_path)
    cache.save_section_embeddings(
        str(pdf_path),
        {0: np.ones(384, dtype="float32").tobytes()},
        {0: "S000:p1:A"},
        model="fake",
    )

    sections = [
        {"id": 0, "key": "S000:p1:A", "text": "alpha"},
        {"id": 1, "key": "S001:p2:B", "text": "beta"},
    ]

    class FakeEmbedder:
        def __init__(self):
            self.calls: list[list[str]] = []

        def embed(self, texts):
            self.calls.append(list(texts))
            return [np.ones(384, dtype="float32") for _ in texts]

    fe = FakeEmbedder()
    embed_sections_for_pdf(cache, str(pdf_path), sections, fe, model_name="fake")
    # Only section 1 should have been embedded.
    assert fe.calls == [["beta"]]
