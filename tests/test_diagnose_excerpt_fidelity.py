"""Tests for scripts/diagnose_excerpt_fidelity.py pure helpers."""

import json

import pytest

from scripts.diagnose_excerpt_fidelity import Question, classify, figures, load_dataset

FACT = "Greater China net sales decreased 8% to $66.9 billion during 2024"


def _write(tmp_path, name, payload):
    (tmp_path / name).write_text(json.dumps(payload))
    return tmp_path


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


class TestLoadDataset:
    def test_reads_the_financial_schema(self, tmp_path):
        _write(
            tmp_path,
            "answerability_questions.json",
            {
                "questions": [
                    {
                        "id": "q1",
                        "scope": "single-doc",
                        "type": "figure",
                        "question": "how did Greater China net sales move?",
                        "expect_docs": ["aapl-fy2024"],
                        "reference_facts": ["net sales decreased 8%"],
                    }
                ]
            },
        )
        got = load_dataset(tmp_path)
        assert got == [
            Question(
                id="q1",
                type="figure",
                question="how did Greater China net sales move?",
                doc="aapl-fy2024",
                fact="net sales decreased 8%",
                span=None,
            )
        ]

    def test_drops_multi_doc_questions(self, tmp_path):
        _write(
            tmp_path,
            "answerability_questions.json",
            {
                "questions": [
                    {
                        "id": "q1",
                        "scope": "multi-doc",
                        "type": "trend",
                        "question": "compare a and b",
                        "expect_docs": ["a", "b"],
                        "reference_facts": ["x"],
                    }
                ]
            },
        )
        assert load_dataset(tmp_path) == []

    def test_reads_the_arxiv_schema_with_a_span(self, tmp_path):
        _write(
            tmp_path,
            "fidelity_questions.json",
            {
                "questions": [
                    {
                        "id": "described-07",
                        "type": "method",
                        "question": "does it need labeled data at inference time",
                        "expect_doc": "0707.1301",
                        "reference_fact": "requires no labeled examples at test time",
                        "answer_span": "no labeled examples at test time",
                    }
                ]
            },
        )
        got = load_dataset(tmp_path)
        assert got == [
            Question(
                id="described-07",
                type="method",
                question="does it need labeled data at inference time",
                doc="0707.1301",
                fact="requires no labeled examples at test time",
                span="no labeled examples at test time",
            )
        ]

    def test_arxiv_questions_need_no_scope_field(self, tmp_path):
        _write(
            tmp_path,
            "fidelity_questions.json",
            {
                "questions": [
                    {
                        "id": "d1",
                        "type": "method",
                        "question": "q",
                        "expect_doc": "x",
                        "reference_fact": "f",
                        "answer_span": "f",
                    }
                ]
            },
        )
        assert len(load_dataset(tmp_path)) == 1

    def test_raises_when_no_questions_file_exists(self, tmp_path):
        with pytest.raises(SystemExit):
            load_dataset(tmp_path)
