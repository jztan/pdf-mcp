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


def test_parse_entries_extracts_id_and_title():
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><id>http://arxiv.org/abs/2401.00001v1</id>"
        "<title>Deep  Nets</title></entry></feed>"
    )
    got = fcd.parse_entries(xml)
    assert got == [{"id": "2401.00001v1", "title": "Deep  Nets"}]


def test_api_endpoint_is_https():
    assert fcd.API.startswith("https://")


import json as _json  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402

sys.path.insert(0, str(REPO / "scripts"))
import benchmark_corpus_modes as bcm  # noqa: E402


def test_load_distractor_paths_keeps_only_existing(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-")
    man = tmp_path / "d.json"
    man.write_text(
        _json.dumps(
            {
                "docs": [
                    {"id": "x", "path": str(pdf)},
                    {"id": "missing", "path": str(tmp_path / "nope.pdf")},
                ]
            }
        )
    )
    got = bcm.load_distractor_paths(man, REPO)
    assert got == [str(pdf)]


def test_apply_cap_raises_but_never_lowers():
    mod = types.SimpleNamespace(CORPUS_MAX_FILES=100)
    bcm.apply_cap(mod, 500)
    assert mod.CORPUS_MAX_FILES == 500
    bcm.apply_cap(mod, 50)
    assert mod.CORPUS_MAX_FILES == 500


def test_build_ranked_keeps_distractor_with_synthetic_id():
    id_by_path = {"/gold/a.pdf": "0705.4297"}
    matches = [
        {"path": "/gold/a.pdf", "page": 3},
        {"path": "/dist/x.pdf", "page": 1},  # distractor, not in id_by_path
    ]
    got = bcm.build_ranked(matches, id_by_path)
    assert got == [("0705.4297", 3), ("/dist/x.pdf", 1)]
    # the distractor id is a filesystem path, never equal to a gold arXiv id
    assert got[1][0] not in id_by_path.values()
