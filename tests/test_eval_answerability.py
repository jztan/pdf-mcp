"""Tests for the answerability eval's follow-up selection (pure logic).

The rule must be one an agent could actually follow: it may look only at
the response (`matches` + `doc_match_counts`), never at the eval's
expected-document list. A rule that peeked at ground truth would measure
an agent that already knows the answer.
"""

from unittest.mock import patch

from scripts.eval_financial_answerability import (
    JUDGE_CONTEXT_FLAGS,
    ballots_decided,
    followup_docs,
    judge_one,
)


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


class TestJudgeContextFlags:
    """The judge subprocess must not reload context it cannot use.

    Measured on this project: 20,704 fresh input tokens per call, of which
    the rubric and payload were 2,495. Dropping CLAUDE.md and the MCP tool
    schemas more than halves the cost of every eval run, and a run makes
    hundreds of calls -- so losing these flags in a refactor is expensive
    and silent. The command is asserted, not the saving.
    """

    QUESTION = {"question": "q", "reference_facts": ["f"], "confusable_with": ""}

    def _argv(self) -> list[str]:
        with patch("scripts.eval_financial_answerability.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"answerable": "full"}'
            judge_one(self.QUESTION, "payload", "claude-opus-4-8")
        return run.call_args[0][0]

    def test_claude_md_is_not_loaded_into_the_judge(self):
        argv = self._argv()
        assert "--setting-sources" in argv
        assert argv[argv.index("--setting-sources") + 1] == ""

    def test_mcp_servers_are_not_loaded_into_the_judge(self):
        argv = self._argv()
        assert "--strict-mcp-config" in argv
        assert '{"mcpServers":{}}' in argv

    def test_the_default_system_prompt_is_kept(self):
        """Replacing it measured MORE expensive (23,680 fresh tokens, zero
        cache reads) because a custom prompt busts the shared cache prefix."""
        assert "--system-prompt" not in self._argv()

    def test_every_context_flag_reaches_the_subprocess(self):
        argv = self._argv()
        for flag in JUDGE_CONTEXT_FLAGS:
            assert flag in argv


class TestBallotsDecided:
    """Early stopping must be LOSSLESS: it may skip a ballot only when that
    ballot could not change the majority-of-N verdict. Simulated against the
    204 recorded ballot triples, this rule costs 2.11 calls per question
    instead of 3 and changes zero verdicts. A cheaper rule that stops on a
    single `full` ballot was rejected: it graded 14 `partial` and 4 `no`
    questions as `full`, biasing the headline number upward.
    """

    def test_two_agreeing_ballots_settle_a_majority_of_three(self):
        assert ballots_decided(["full", "full"], [False, False], votes=3)

    def test_two_disagreeing_ballots_do_not(self):
        assert not ballots_decided(["full", "partial"], [False, False], votes=3)

    def test_a_single_ballot_never_settles_anything(self):
        assert not ballots_decided(["full"], [False], votes=3)

    def test_the_final_ballot_always_settles(self):
        assert ballots_decided(["full", "partial", "no"], [False] * 3, votes=3)

    def test_agreement_on_answerable_is_not_enough_when_wrong_is_open(self):
        """wrong_attribution is tallied separately and needs its own majority.
        Two ballots agreeing on `full` while splitting on the trap flag leave
        the trap undecided -- a third ballot could still carry it."""
        assert not ballots_decided(["full", "full"], [True, False], votes=3)

    def test_both_dimensions_agreeing_settles(self):
        assert ballots_decided(["full", "full"], [True, True], votes=3)

    def test_wrong_cannot_reach_a_majority_once_two_ballots_deny_it(self):
        assert ballots_decided(["no", "no"], [False, False], votes=3)


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
