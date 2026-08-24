import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fetch_corpus_distractors", REPO / "scripts" / "fetch_corpus_distractors.py"
)
fcd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fcd)


def test_gold_base_ids_strips_version():
    gold = {"docs": [{"id": "0705.4297"}, {"id": "0706.0028v2"}]}
    assert fcd.gold_base_ids(gold) == {"0705.4297", "0706.0028"}


def test_dedup_removes_gold_id_and_version():
    cands = [
        {"id": "0705.4297", "title": "A"},  # exact gold id
        {"id": "0706.0028v3", "title": "B"},  # version of a gold id
        {"id": "2401.00001", "title": "New"},  # keep
    ]
    kept = fcd.dedup_candidates(cands, {"0705.4297", "0706.0028"}, set())
    assert [c["id"] for c in kept] == ["2401.00001"]


def test_dedup_removes_title_match_case_insensitive():
    cands = [{"id": "2401.00002", "title": "Deep  Nets"}]
    kept = fcd.dedup_candidates(cands, set(), {"deep nets"})
    assert kept == []


def test_arxiv_pdf_url():
    assert fcd.arxiv_pdf_url("2401.00001") == "https://arxiv.org/pdf/2401.00001"
