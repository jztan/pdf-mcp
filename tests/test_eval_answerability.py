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
        # Isolate from the ballot cache in BOTH directions: a cached ballot
        # would skip the subprocess entirely (call_args = None), and an
        # unpatched _record_ballot would write this mocked verdict into the
        # PRODUCTION cache file -- which happened, and poisoned later runs.
        with (
            patch(
                "scripts.eval_financial_answerability._take_cached_ballot",
                return_value=None,
            ),
            patch("scripts.eval_financial_answerability._record_ballot"),
            patch("scripts.eval_financial_answerability.subprocess.run") as run,
        ):
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


class TestPagesToRead:
    """Which pages a decomposed run opens.

    Must come from the response's own ranking only. Selecting by the
    eval's known-good page would measure an agent that already had the
    answer, which is the same trap `followup_docs` guards against.
    """

    @staticmethod
    def _fn():
        from scripts.eval_single_doc_answerability import pages_to_read

        return pages_to_read

    def test_takes_the_best_ranked_pages_in_order(self):
        matches = [{"page": 7}, {"page": 3}, {"page": 9}]
        assert self._fn()(matches, limit=2) == [7, 3]

    def test_deduplicates_repeated_pages(self):
        """Several excerpts often come from one page; that is one page to open."""
        matches = [{"page": 7}, {"page": 7}, {"page": 3}]
        assert self._fn()(matches, limit=2) == [7, 3]

    def test_empty_matches_open_nothing(self):
        assert self._fn()([], limit=2) == []


class TestBallotCache:
    """A paid-for ballot is reused; a stale one never is.

    A run killed 26 questions in threw away 53 completed judge calls
    because verdicts were written only at the end. Ballots are now
    appended as they complete and replayed on the next run.
    """

    @staticmethod
    def _mod():
        import scripts.eval_financial_answerability as mod

        return mod

    def test_the_key_changes_when_the_payload_changes(self):
        mod = self._mod()
        a = mod._cache_key("prompt A", "claude-opus-4-8")
        b = mod._cache_key("prompt B", "claude-opus-4-8")
        assert a != b

    def test_the_key_changes_when_the_model_changes(self):
        """A ballot from one judge must never be served for another."""
        mod = self._mod()
        a = mod._cache_key("same prompt", "claude-opus-4-8")
        b = mod._cache_key("same prompt", "claude-haiku-4-5")
        assert a != b

    def test_surrounding_whitespace_does_not_change_the_key(self):
        mod = self._mod()
        assert mod._cache_key("p", "m") == mod._cache_key("  p\n", "m")

    def test_a_cached_ballot_is_used_instead_of_spending_a_call(self):
        mod = self._mod()
        question = {"question": "q", "reference_facts": ["f"], "confusable_with": ""}
        with (
            patch.object(mod, "_take_cached_ballot") as take,
            patch.object(mod, "subprocess") as sub,
        ):
            take.return_value = {"answerable": "full", "wrong_attribution": False}
            out = mod.judge_one(question, "payload", "claude-opus-4-8")
        assert out["answerable"] == "full"
        sub.run.assert_not_called()

    def test_a_fresh_ballot_is_written_to_the_cache(self):
        """The half that was missing: reads were wired, writes were not.

        `_record_ballot` existed and was never called, so a run spent
        hundreds of calls and saved none of them -- while the two tests
        above still passed, because one mocked the reader and the other
        poked the cache dict directly. Neither exercised the write path.
        """
        mod = self._mod()
        question = {"question": "q", "reference_facts": ["f"], "confusable_with": ""}
        with (
            patch.object(mod, "_take_cached_ballot", return_value=None),
            patch.object(mod, "_record_ballot") as record,
            patch.object(mod, "subprocess") as sub,
        ):
            sub.run.return_value.returncode = 0
            sub.run.return_value.stdout = '{"answerable": "full"}'
            mod.judge_one(question, "payload", "claude-opus-4-8")
        record.assert_called_once()
        assert record.call_args[0][1]["answerable"] == "full"

    def test_an_error_ballot_is_not_cached(self):
        """Caching a transient failure would replay it forever."""
        mod = self._mod()
        question = {"question": "q", "reference_facts": ["f"], "confusable_with": ""}
        with (
            patch.object(mod, "_take_cached_ballot", return_value=None),
            patch.object(mod, "_record_ballot") as record,
            patch.object(mod, "subprocess") as sub,
        ):
            sub.run.return_value.returncode = 1
            sub.run.return_value.stdout = ""
            mod.judge_one(question, "payload", "claude-opus-4-8")
        record.assert_not_called()

    def test_each_cached_ballot_is_consumed_once(self):
        """Otherwise a majority of 3 becomes one ballot counted three times."""
        mod = self._mod()
        mod._BALLOT_CACHE = {"k": [{"answerable": "full"}, {"answerable": "no"}]}
        first = mod._take_cached_ballot("k")
        second = mod._take_cached_ballot("k")
        third = mod._take_cached_ballot("k")
        assert first != second
        assert third is None
        mod._BALLOT_CACHE = None
