"""Tests for the document-profile benchmark tooling (census, cover variants)."""

import json

import pymupdf

from pdf_mcp.cache import PDFCache
from scripts.doc_profile_census import census, compare_caches, head_page
from scripts.make_cover_variant_dataset import build_variant


def _pdf(path, pages):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return str(path)


def _warm(cache, path, texts):
    cache.save_metadata(path, len(texts), {}, [])
    cache.save_pages_text(path, dict(enumerate(texts)))


class TestHeadPage:
    def test_skips_whitespace_only_pages(self):
        assert head_page({0: " \n\t", 1: "", 2: "Title"}) == 2

    def test_minus_one_when_no_text(self):
        assert head_page({0: " ", 1: "\f"}) == -1


def test_census_counts_empty_page1_and_thin_heads(tmp_path):
    body = "Body text that is comfortably longer than fifty characters in total."
    docs = {
        "a": ["Page one has real text. " + body, body],
        "b": ["", body],
        "c": [" \n", "Annual Report 2024", body],
    }
    cache = PDFCache(cache_dir=tmp_path / "cache", ttl_hours=24)
    paths = []
    for name, texts in docs.items():
        path = _pdf(tmp_path / f"{name}.pdf", ["x"] * len(texts))
        _warm(cache, path, texts)
        paths.append(path)

    out = census(cache, paths)

    assert out["docs"] == 3
    assert out["empty_page1"] == 2
    assert out["head_page_under_50"] == 1
    assert out["head_page_counts"] == {"0": 1, "1": 2}


def test_compare_caches_flags_differing_text(tmp_path):
    path = _pdf(tmp_path / "d.pdf", ["x", "y"])
    a = PDFCache(cache_dir=tmp_path / "a", ttl_hours=24)
    b = PDFCache(cache_dir=tmp_path / "b", ttl_hours=24)
    _warm(a, path, ["one", "two"])
    _warm(b, path, ["one", "two"])
    assert compare_caches(a, b, [path]) == {"compared": 1, "differ": []}
    b.save_pages_text(path, {1: "changed"})
    assert compare_caches(a, b, [path])["differ"] == [path]


def test_cover_variant_shifts_gold_pages_by_two(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    a = _pdf(src / "a.pdf", ["alpha one", "alpha two", "alpha three"])
    b = _pdf(src / "b.pdf", ["beta one", "beta two"])
    (src / "manifest.json").write_text(
        json.dumps(
            {
                "description": "tiny",
                "docs": [
                    {"id": "a", "path": a, "lang": "en"},
                    {"id": "b", "path": b, "lang": "en"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (src / "queries.json").write_text(
        json.dumps(
            {
                "queries": [
                    {
                        "id": "q1",
                        "class": "exact_match",
                        "query": "alpha three",
                        "labels": [
                            {"doc": "a", "page": 3, "gain": 2, "evidence": "alpha"}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"

    chosen = build_variant(src, out, 1, seed=1)

    assert chosen == ["a"]  # only doc a has a graded query
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    paths = {d["id"]: d["path"] for d in manifest["docs"]}
    assert paths["b"] == b
    variant = pymupdf.open(paths["a"])
    assert variant.page_count == 3 + 2
    assert variant[0].get_text().strip() == ""
    assert variant[1].get_text().strip() == ""
    assert variant[1].get_images()  # raster, no text layer
    assert variant[2].get_text().strip() == "alpha one"
    variant.close()
    queries = json.loads((out / "queries.json").read_text(encoding="utf-8"))
    assert queries["queries"][0]["labels"][0]["page"] == 5
    again = build_variant(src, tmp_path / "out2", 1, seed=1)
    assert again == chosen


def test_cache_dir_flag_parses_in_both_benchmarks(tmp_path):
    from scripts import benchmark_corpus_modes, diagnose_excerpt_fidelity

    for module in (benchmark_corpus_modes, diagnose_excerpt_fidelity):
        parser = module.build_parser()
        assert parser.parse_args([]).cache_dir is None
        args = parser.parse_args(["--cache-dir", str(tmp_path)])
        assert args.cache_dir == tmp_path
