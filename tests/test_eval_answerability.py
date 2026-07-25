"""Tests for the answerability eval's follow-up selection (pure logic).

The rule must be one an agent could actually follow: it may look only at
the response (`matches` + `doc_match_counts`), never at the eval's
expected-document list. A rule that peeked at ground truth would measure
an agent that already knows the answer.
"""

from scripts.eval_financial_answerability import followup_docs


class TestFollowupDocs:
    def test_selects_documents_that_matched_but_won_no_slot(self):
        matches = [{"path": "a.pdf"}, {"path": "a.pdf"}]
        counts = {"a.pdf": 5, "b.pdf": 3}
        assert followup_docs(matches, counts, limit=3) == ["b.pdf"]

    def test_returns_nothing_when_every_matching_doc_is_represented(self):
        matches = [{"path": "a.pdf"}, {"path": "b.pdf"}]
        counts = {"a.pdf": 5, "b.pdf": 3}
        assert followup_docs(matches, counts, limit=3) == []

    def test_orders_by_match_count_descending(self):
        counts = {"a.pdf": 1, "b.pdf": 9, "c.pdf": 4}
        assert followup_docs([], counts, limit=3) == ["b.pdf", "c.pdf", "a.pdf"]

    def test_respects_the_limit(self):
        counts = {"a.pdf": 1, "b.pdf": 9, "c.pdf": 4}
        assert followup_docs([], counts, limit=2) == ["b.pdf", "c.pdf"]

    def test_empty_counts_yields_no_followups(self):
        """The pre-fix hybrid behaviour: doc_match_counts came back {}, so an
        agent had no signal to follow up on and the flow could not start."""
        assert followup_docs([{"path": "a.pdf"}], {}, limit=3) == []


class TestQuestionScopeField:
    """`scope` is what both evals filter on, so it must never disagree with
    expect_docs. Ids are opaque: the sd- prefix on some of them records when
    they were added, not their scope, and 9 single-doc questions predate it."""

    @staticmethod
    def _questions():
        import json
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "benchmark_data/financial_reports/answerability_questions.json"
        )
        return json.loads(path.read_text())["questions"]

    def test_every_question_declares_a_scope(self):
        for q in self._questions():
            assert q.get("scope") in ("single-doc", "multi-doc"), q["id"]

    def test_scope_agrees_with_expect_docs(self):
        for q in self._questions():
            expected = "single-doc" if len(q["expect_docs"]) == 1 else "multi-doc"
            assert (
                q["scope"] == expected
            ), f"{q['id']}: scope={q['scope']} but {len(q['expect_docs'])} docs"

    def test_id_prefix_is_not_used_as_a_scope_marker(self):
        """Guards the trap that prompted this field: filtering by the sd-
        prefix silently returns 16 of the 25 single-doc questions."""
        qs = self._questions()
        by_scope = [q for q in qs if q["scope"] == "single-doc"]
        by_prefix = [q for q in qs if q["id"].startswith("sd-")]
        assert len(by_scope) > len(
            by_prefix
        ), "if these ever match, someone has re-coupled ids to scope"
