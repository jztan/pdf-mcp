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
