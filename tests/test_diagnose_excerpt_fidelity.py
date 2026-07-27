"""Tests for scripts/diagnose_excerpt_fidelity.py pure helpers."""

from scripts.diagnose_excerpt_fidelity import classify, figures

FACT = "Greater China net sales decreased 8% to $66.9 billion during 2024"


class TestFiguresUnchanged:
    def test_keeps_qualified_numerics_only(self):
        assert figures("8% to $66.9 billion in 2024") == {"8", "66.9"}


class TestClassifyWithoutSpan:
    def test_carries_when_excerpt_repeats_the_figure(self):
        quotes, carries = classify(FACT, ["net sales fell to $66.9 billion"])
        assert carries is True

    def test_not_carries_when_figure_absent(self):
        quotes, carries = classify(FACT, ["net sales fell in the region"])
        assert carries is False

    def test_falls_back_to_the_word_window_when_fact_has_no_figure(self):
        fact = "the prototypes are computed offline before any inference runs"
        quotes, carries = classify(fact, [fact])
        assert quotes is True and carries is True


class TestClassifyWithSpan:
    def test_carries_when_span_present(self):
        fact = "requires no labeled examples at test time, computed offline"
        _, carries = classify(
            fact,
            ["the model requires no labeled examples at test time"],
            span="no labeled examples at test time",
        )
        assert carries is True

    def test_not_carries_when_span_absent(self):
        fact = "requires no labeled examples at test time, computed offline"
        _, carries = classify(
            fact,
            ["the model is trained with a contrastive objective"],
            span="no labeled examples at test time",
        )
        assert carries is False

    def test_span_ignores_whitespace_and_case_differences(self):
        fact = "requires no labeled examples at test time, computed offline"
        _, carries = classify(
            fact,
            ["Requires  NO labeled\nexamples at test time"],
            span="no labeled examples at test time",
        )
        assert carries is True

    def test_span_path_does_not_consult_figures(self):
        # The fact carries a figure the excerpt lacks; the span is present.
        # Span wins, because the span is what the question turns on.
        fact = "trained for 90 epochs with no labeled examples at test time"
        _, carries = classify(
            fact,
            ["the method uses no labeled examples at test time"],
            span="no labeled examples at test time",
        )
        assert carries is True
