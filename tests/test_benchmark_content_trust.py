import importlib.util
import os
import tempfile

import pymupdf

from pdf_mcp.content_trust import scan_document


def _gen():
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "benchmark_data",
        "content_trust_corpus",
        "generate.py",
    )
    spec = importlib.util.spec_from_file_location("ct_gen", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_corpus_has_zero_misclassifications():
    gen = _gen()
    with tempfile.TemporaryDirectory() as d:
        for name, label in gen.build(d):
            doc = pymupdf.open(os.path.join(d, name))
            try:
                suspicious = scan_document(doc)["suspicious"]
            finally:
                doc.close()
            assert suspicious == (label == "attack"), name
