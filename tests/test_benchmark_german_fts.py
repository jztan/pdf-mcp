import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import benchmark_german_fts as bench  # noqa: E402


def test_german_mode_beats_porter_default_on_inflected_and_spelling_queries():
    results = bench.run_benchmark()
    porter, de = results["porter"], results["de"]

    # Headline regression guard: "de" mode must not be worse than porter on
    # any individual query (never regresses what already worked).
    for query, row in de["per_query"].items():
        assert row["recall"] >= porter["per_query"][query]["recall"], query

    # And it must be a real improvement, not a no-op.
    assert de["mean_recall"] > porter["mean_recall"]
    assert de["mrr"] > porter["mrr"]

    # Specific cases the option exists for.
    assert de["per_query"]["kündigen"]["recall"] == 1.0
    assert de["per_query"]["Kuendigung"]["recall"] == 1.0
    assert de["per_query"]["Strasse"]["recall"] == 1.0
