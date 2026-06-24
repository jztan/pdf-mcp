import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # scripts/archive (this dir)
sys.path.insert(0, str(Path(__file__).parent.parent))  # scripts/ (active modules)


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


def test_hybrid_section_search_fuses_keyword_and_semantic(tmp_path):
    """BM25 ranks section 1 first (lexical match); semantic ranks
    section 2 first (vector closest); RRF should put both in top-2."""
    from pdf_mcp.cache import PDFCache
    from pdf_mcp.section_detector import Section
    from benchmark_hybrid_sections import hybrid_section_search
    import numpy as np

    cache = PDFCache(cache_dir=tmp_path)
    pdf_path = tmp_path / "x.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    sections = [
        Section(
            title="Intro",
            start_page=1,
            end_page=1,
            text="introduction to graphs",
        ),
        Section(
            title="Methods",
            start_page=2,
            end_page=2,
            text="convolutional neural networks",
        ),
        Section(
            title="Results",
            start_page=3,
            end_page=3,
            text="experimental analysis",
        ),
        Section(
            title="Conclusion",
            start_page=4,
            end_page=4,
            text="summary and future work",
        ),
    ]
    cache.index_sections(str(pdf_path), sections)

    # Embeddings: query is closest to section 2 in vector space.
    embeddings = {
        0: np.array([0.1] + [0.0] * 383, dtype="float32"),
        1: np.array([0.3] + [0.0] * 383, dtype="float32"),
        2: np.array([0.9] + [0.0] * 383, dtype="float32"),  # closest
        3: np.array([0.2] + [0.0] * 383, dtype="float32"),
    }
    cache.save_section_embeddings(
        str(pdf_path),
        {k: v.tobytes() for k, v in embeddings.items()},
        {k: f"S{k:03d}:p{k+1}:T" for k in range(4)},
        model="m",
    )

    query_vec = np.array([1.0] + [0.0] * 383, dtype="float32")
    # BM25 will match "convolutional" → section 1.
    # Cosine puts section 2 first.
    ranked_ids = hybrid_section_search(
        cache, str(pdf_path), "convolutional", query_vec, top_k=4
    )
    assert set(ranked_ids[:2]) == {1, 2}


def _baseline_cells():
    """Return a baseline 4-cell scoring where all clauses pass."""
    return {
        "keyword-page": {
            "lexical": 0.70,
            "paraphrase-semantic": 0.40,
            "mixed-distractor": 0.30,
            "all": 0.47,
        },
        "keyword-section": {
            "lexical": 0.85,
            "paraphrase-semantic": 0.50,
            "mixed-distractor": 0.40,
            "all": 0.58,
        },
        "hybrid-page": {
            "lexical": 0.75,
            "paraphrase-semantic": 0.60,
            "mixed-distractor": 0.50,
            "all": 0.62,
        },
        "hybrid-section": {
            "lexical": 0.82,
            "paraphrase-semantic": 0.70,
            "mixed-distractor": 0.65,
            "all": 0.72,
        },
    }


def test_gate_passes_when_all_clauses_met():
    from benchmark_hybrid_sections import evaluate_gate

    v = evaluate_gate(_baseline_cells())
    assert v["pass"] is True
    assert v["clause_1_mixed_distractor"]["pass"] is True
    assert v["clause_2_lexical"]["pass"] is True
    assert v["clause_3_overall"]["pass"] is True


def test_gate_fails_clause_1_mixed_distractor_margin():
    from benchmark_hybrid_sections import evaluate_gate

    cells = _baseline_cells()
    # Hybrid-page mixed-distractor catches up to 0.60 → margin 0.05 < 0.10.
    cells["hybrid-page"]["mixed-distractor"] = 0.60
    v = evaluate_gate(cells)
    assert v["pass"] is False
    assert v["clause_1_mixed_distractor"]["pass"] is False


def test_gate_fails_clause_2_lexical_regression():
    from benchmark_hybrid_sections import evaluate_gate

    cells = _baseline_cells()
    # Drop hybrid-section lexical 0.10 below keyword-section.
    cells["hybrid-section"]["lexical"] = 0.74
    v = evaluate_gate(cells)
    assert v["pass"] is False
    assert v["clause_2_lexical"]["pass"] is False


def test_gate_fails_clause_3_overall_below_hybrid_page():
    from benchmark_hybrid_sections import evaluate_gate

    cells = _baseline_cells()
    cells["hybrid-section"]["all"] = 0.61  # hybrid-page is 0.62
    v = evaluate_gate(cells)
    assert v["pass"] is False
    assert v["clause_3_overall"]["pass"] is False


def test_gate_clause_1_uses_next_best_not_just_hybrid_page():
    """If keyword-section happens to beat hybrid-page on mixed-distractor,
    that's the cell hybrid-section must beat by 0.10."""
    from benchmark_hybrid_sections import evaluate_gate

    cells = _baseline_cells()
    cells["hybrid-page"]["mixed-distractor"] = 0.30  # lower
    cells["keyword-section"]["mixed-distractor"] = 0.60  # now next-best
    cells["hybrid-section"]["mixed-distractor"] = 0.65  # margin only 0.05
    v = evaluate_gate(cells)
    assert v["clause_1_mixed_distractor"]["pass"] is False


def test_gate_reports_next_best_cell_name():
    """When keyword-section is the next-best on mixed-distractor, that
    name must appear in the verdict so the spec §7 open question
    ('is keyword-section beating hybrid-page?') is answerable from the
    verdict alone."""
    from benchmark_hybrid_sections import evaluate_gate

    cells = _baseline_cells()
    cells["hybrid-page"]["mixed-distractor"] = 0.30
    cells["keyword-section"]["mixed-distractor"] = 0.55
    v = evaluate_gate(cells)
    assert v["clause_1_mixed_distractor"]["next_best_cell"] == "keyword-section"


def test_run_all_cells_end_to_end(tmp_path):
    """Build a tiny synthetic PDF with two clearly-different sections,
    run the benchmark on a one-query-per-category fixture, and assert
    every cell × category produces a finite MRR in [0, 1]."""
    import pymupdf
    import numpy as np
    import json
    from benchmark_hybrid_sections import (
        run_all_cells,
        load_queries,
        section_key,
    )
    from pdf_mcp.section_detector import derive_sections

    # 1. Build a 2-page PDF: section A about cats, section B about dogs.
    pdf_path = tmp_path / "tiny.pdf"
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((50, 50), "Section A: Cats", fontsize=20)
    p1.insert_text((50, 100), "Felines purr and meow loudly")
    p2 = doc.new_page()
    p2.insert_text((50, 50), "Section B: Dogs", fontsize=20)
    p2.insert_text((50, 100), "Canines bark and fetch sticks")
    doc.save(str(pdf_path))
    doc.close()

    # 2. Compute section keys exactly as the benchmark would.
    sections = derive_sections(str(pdf_path))
    if len(sections) < 2:
        # Heuristic detector may not split this trivial PDF; bail with
        # a clear assertion so it's obvious in CI.
        raise AssertionError(
            f"Synthetic PDF only produced {len(sections)} sections; "
            "test fixture needs adjustment."
        )
    key_for = {i: section_key(i, s) for i, s in enumerate(sections)}
    cats_key = key_for[0]

    # 3. Author a query fixture pointing at the local PDF.
    qfile = tmp_path / "q.json"
    qfile.write_text(
        json.dumps(
            {
                "pdfs": {
                    "tiny": {
                        "url": str(pdf_path),
                        "queries": [
                            {
                                "id": "tlex",
                                "category": "lexical",
                                "query": "Cats",
                                "gold_section_keys": [cats_key],
                            },
                            {
                                "id": "tsem",
                                "category": "paraphrase-semantic",
                                "query": "felines",
                                "gold_section_keys": [cats_key],
                            },
                            {
                                "id": "tdist",
                                "category": "mixed-distractor",
                                "query": "purr",
                                "gold_section_keys": [cats_key],
                                "distractor_section_keys": [key_for[1]],
                            },
                        ],
                    }
                }
            }
        )
    )
    pdfs = load_queries(str(qfile))

    # 4. Stub embedder mapping cat-words to dim 0, dog-words to dim 1.
    class StubEmbedder:
        def embed(self, texts):
            for t in texts:
                v = np.zeros(384, dtype="float32")
                tl = t.lower()
                if "cat" in tl or "felin" in tl or "purr" in tl or "meow" in tl:
                    v[0] = 1.0
                elif "dog" in tl or "canin" in tl or "bark" in tl:
                    v[1] = 1.0
                yield v

    cells = run_all_cells(pdfs, embedder=StubEmbedder())

    # 5. Assert shape + finite values.
    for cell_name in (
        "keyword-page",
        "keyword-section",
        "hybrid-page",
        "hybrid-section",
    ):
        for category in ("lexical", "paraphrase-semantic", "mixed-distractor", "all"):
            score = cells[cell_name][category]
            assert 0.0 <= score <= 1.0, f"{cell_name}/{category} = {score} out of [0,1]"
