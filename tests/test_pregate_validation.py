import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))

CORPUS = REPO / "benchmark_data" / "pregate_validation_corpus.json"


@pytest.mark.slow
def test_non_arxiv_two_column_pages_are_not_short_circuited():
    import pymupdf

    from pdf_mcp.extractor import is_confidently_single_column
    from pdf_mcp.server import _resolve_path

    corpus = json.loads(CORPUS.read_text("utf-8"))
    for entry in corpus["must_not_short_circuit"]:
        path, err = _resolve_path(entry["url"])  # (path, None) | (None, error)
        assert not err, f"resolve failed for {entry['url']}: {err}"
        doc = pymupdf.open(path)
        page = doc[entry["page"] - 1]
        blocks = page.get_text("blocks", sort=True)
        assert is_confidently_single_column(blocks) is False, (
            f"{entry['layout']} p{entry['page']} wrongly short-circuited"
        )
        doc.close()
