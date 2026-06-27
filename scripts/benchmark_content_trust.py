"""Benchmark hidden-text detection on the synthetic corpus.

Run: uv run python scripts/benchmark_content_trust.py
Reports precision/recall on `suspicious` and lists every misclassified file.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile

import pymupdf

from pdf_mcp.content_trust import scan_document


def _load_generator():
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


def main() -> None:
    gen = _load_generator()
    with tempfile.TemporaryDirectory() as d:
        specs = gen.build(d)
        tp = fp = tn = fn = 0
        misses = []
        for name, label in specs:
            doc = pymupdf.open(os.path.join(d, name))
            try:
                suspicious = scan_document(doc)["suspicious"]
            finally:
                doc.close()
            expected = label == "attack"
            if suspicious and expected:
                tp += 1
            elif suspicious and not expected:
                fp += 1
                misses.append(f"FALSE POSITIVE: {name}")
            elif not suspicious and expected:
                fn += 1
                misses.append(f"MISS (false negative): {name}")
            else:
                tn += 1
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        recall = tp / (tp + fn) if (tp + fn) else 1.0
        print(f"corpus: {len(specs)} files  TP={tp} FP={fp} TN={tn} FN={fn}")
        print(f"precision={precision:.3f}  recall={recall:.3f}")
        for m in misses:
            print(m)
        if fp or fn:
            raise SystemExit("benchmark has misclassifications — tune thresholds")


if __name__ == "__main__":
    main()
