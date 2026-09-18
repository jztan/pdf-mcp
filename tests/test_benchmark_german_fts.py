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

    # Numbers and statute citations (digit-dropping tokenizer regression):
    # a numeric or mixed alphanumeric query must still hit its page under
    # "de" mode -- porter (English) can already match these literally, so
    # only "de" recall is asserted here.
    assert de["per_query"]["626"]["recall"] == 1.0
    assert de["per_query"]["§ 626 BGB"]["recall"] == 1.0
    assert de["per_query"]["2023"]["recall"] == 1.0

    # Multi-word query: AND-semantics must still exclude the distractor
    # page that only shares one of the two words.
    assert de["per_query"]["befristeter Arbeitsvertrag"]["recall"] == 1.0

    # OR-fallback regression (PR #44 round 2): no page has both "622" and
    # "bgb", so only the OR retry finds the true citation.
    assert de["per_query"]["§ 622 BGB"]["recall"] == 1.0

    # "626" must still discriminate from "1626" now that a page citing
    # "§ 1626" (page 12) is also in the corpus.
    assert de["per_query"]["626"]["returned"] == [8]
