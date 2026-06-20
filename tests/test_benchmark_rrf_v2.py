import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

REPO = Path(__file__).parent.parent
CORPUS = REPO / "benchmark_data" / "rrf_v2_queries.json"
GROUND_TRUTH = REPO / "benchmark_data" / "ground_truth.json"

_CLASSES = {"stemming", "substring", "fusion", "distractor"}


def _load_corpus():
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def _load_ground_truth():
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["pdfs"]


def test_corpus_schema_and_coverage():
    corpus = _load_corpus()
    gt = _load_ground_truth()
    queries = corpus["queries"]

    # Size floor for NDCG sensitivity.
    assert len(queries) >= 25, f"need >=25 queries, have {len(queries)}"
    assert len({q["pdf"] for q in queries}) >= 3, "need >=3 distinct PDFs"

    # The gate exists to catch what trigram breaks: stemming + substring.
    by_class = {c: 0 for c in _CLASSES}
    ids = set()
    for q in queries:
        assert q["id"] not in ids, f"duplicate id {q['id']}"
        ids.add(q["id"])
        assert q["class"] in _CLASSES, f"bad class {q['class']}"
        by_class[q["class"]] += 1
        assert q["pdf"] in gt, f"unknown pdf {q['pdf']}"
        page_count = gt[q["pdf"]]["page_count"]
        assert q["query"].strip(), f"empty query {q['id']}"
        assert q["labels"], f"no labels for {q['id']}"
        for page_str, grade in q["labels"].items():
            page = int(page_str)
            assert 1 <= page <= page_count, f"{q['id']} page {page} out of range"
            assert grade in (0, 1, 2, 3), f"{q['id']} bad grade {grade}"

    assert by_class["stemming"] >= 5, "need >=5 stemming-sensitive queries"
    assert by_class["substring"] >= 5, "need >=5 substring-sensitive queries"


sys.path.insert(0, str(REPO / "src"))


def test_rrf_fusion_surfaces_union_of_both_arms():
    # keyword finds {A=1, B=2}; semantic finds {B=2, C=3}; gold = {1,2,3}
    from pdf_mcp.server import _rrf_fuse

    fused = _rrf_fuse([1, 2], [2, 3], 10)
    pages = [p for p, _ in fused]
    assert set(pages) >= {1, 2, 3}
    # B appears in both arms -> highest fused score
    assert pages[0] == 2


def test_rrf_does_not_promote_single_signal_distractor_above_gold():
    # gold page 2 is mid-rank in both arms; distractor 9 tops only keyword.
    from pdf_mcp.server import _rrf_fuse

    fused = _rrf_fuse([9, 5, 2], [2, 5], 10)
    ranks = {p: i for i, (p, _) in enumerate(fused)}
    assert ranks[2] < ranks[9]  # gold outranks the keyword-only distractor


def test_ranked_gains_maps_pages_to_grades_in_rank_order():
    import benchmark_rrf as br

    matches = [{"page": 5}, {"page": 1}, {"page": 9}]
    labels = {"5": 2, "1": 3}  # page 9 unlabelled -> 0
    assert br._ranked_gains(matches, labels) == [2.0, 3.0, 0.0]
