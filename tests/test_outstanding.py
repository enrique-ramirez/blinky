from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from reviewer import log, outstanding, prompt, render
from reviewer.config import REPO_DEFAULTS, RepoConfig, _deep_merge
from reviewer.diff import DiffBundle
from reviewer.gh.graphql import CheckState, PRSnapshot, ReviewThread, ThreadComment
from reviewer.model import ModelError, ModelResult
from reviewer.pipeline import Reviewer
from reviewer.state import PRState, Store

PERSONALITY = Path(__file__).resolve().parent.parent / "personality"
US = "reviewer-bot"
AUTHOR = "alice"
RABBIT = "coderabbitai[bot]"
HEAD = "abc1234567890def"


def config(**approval: Any) -> RepoConfig:
    approval.setdefault("mode", "auto")
    data = _deep_merge(REPO_DEFAULTS, {"approval": approval})
    return RepoConfig(
        repo="acme/widgets",
        owner="acme",
        name="widgets",
        enabled=True,
        local_path=None,
        identity=US,
        language="en",
        agent_language="en",
        model={},
        gates=data["gates"],
        diff=data["diff"],
        review=data["review"],
        approval=data["approval"],
        notify_on=data["notify_on"],
        source_file=Path("/dev/null"),
    )


def comment(author: str, body: str, node_id: str = "c1") -> ThreadComment:
    return ThreadComment(
        node_id=node_id, database_id=1, author=author, body=body, created_at="", url=""
    )


def thread(
    node_id: str,
    *comments: ThreadComment,
    resolved: bool = False,
    outdated: bool = False,
    path: str = "src/App.tsx",
    line: int = 12,
) -> ReviewThread:
    return ReviewThread(
        node_id=node_id,
        is_resolved=resolved,
        is_outdated=outdated,
        path=path,
        line=line,
        original_line=line,
        diff_side="RIGHT",
        comments=list(comments),
    )


def rabbit_thread(node_id: str = "T1", *replies: ThreadComment, **kw: Any) -> ReviewThread:
    return thread(node_id, comment(RABBIT, "**Guard against a null account.**", "r1"), *replies, **kw)


def snapshot(
    threads: list[ReviewThread],
    *,
    head_sha: str = HEAD,
    author: str = AUTHOR,
    labels: list[str] | None = None,
) -> PRSnapshot:
    return PRSnapshot(
        number=7,
        title="Tidy the header",
        is_draft=False,
        review_decision=None,
        head_sha=head_sha,
        base_ref="main",
        additions=10,
        deletions=2,
        changed_files=1,
        author=author,
        labels=labels or [],
        requested_reviewers=[],
        opinionated_reviews=[],
        checks=CheckState(rollup=None),
        threads=threads,
        default_branch="main",
    )


def answer(verdict: str, evidence: str = "src/App.tsx:12 now returns early on null") -> ModelResult:
    return ModelResult(
        payload={"verdict": verdict, "evidence": evidence}, raw="", usage={}, duration_seconds=0.1
    )


def empty_bundle() -> DiffBundle:
    return DiffBundle(
        files=[], excluded=[], summarized=[], dropped_for_size=[], truncated=[],
        reviewable_lines=0, total_lines=0,
    )


class FakeRest:
    """Posting, plus just enough reading for a round that finds nothing new."""

    def __init__(self) -> None:
        self.reviews: list[dict[str, Any]] = []

    def create_review(self, owner: str, repo: str, number: int, **payload: Any) -> Any:
        self.reviews.append(payload)
        return {}

    def list_reviews(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return []

    def get_pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return {"body": "Tidies the header."}

    def list_pull_files(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return [{"filename": "src/App.tsx", "additions": 5, "deletions": 1, "patch": "@@"}]

    def get(self, path: str) -> list[dict[str, Any]]:
        return [{"filename": "src/App.tsx"}]

    def compare_commits(self, owner: str, repo: str, base: str, head: str) -> list[dict[str, Any]]:
        return [{"filename": "from-main.ts", "additions": 5, "deletions": 1, "patch": "@@"}]


class FakeGraphQL:
    def resolve_thread(self, thread_id: str) -> bool:
        return True


class Quiet:
    def write(self, *args: Any) -> None:
        pass


class Globals:
    notifications = {"enabled": False}

    def thread_provider_for(self, cfg: RepoConfig) -> dict[str, Any]:
        return {"type": "fake"}

    provider_for = thread_provider_for


class WithStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name))
        self.rest = FakeRest()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def reviewer(self, cfg: RepoConfig | None = None) -> Reviewer:
        return Reviewer(
            cfg=cfg or config(),
            global_cfg=Globals(),  # type: ignore[arg-type]
            rest=self.rest,  # type: ignore[arg-type]
            graphql=FakeGraphQL(),  # type: ignore[arg-type]
            store=self.store,
            personality_dir=PERSONALITY,
            debug=Quiet(),  # type: ignore[arg-type]
        )

    def publish(self, reviewer: Reviewer, snap: PRSnapshot) -> dict[str, Any]:
        reviewer._publish(
            snapshot=snap, bundle=empty_bundle(), findings=[], summary_text="Tidies the header."
        )
        self.assertEqual(len(self.rest.reviews), 1)
        return self.rest.reviews[0]


class SortingOtherPeoplesThreads(unittest.TestCase):
    def test_a_thread_nobody_answered_is_unanswered(self) -> None:
        parts = outstanding.partition(snapshot([rabbit_thread()]), config())
        self.assertEqual([t.node_id for t in parts.unanswered], ["T1"])
        self.assertEqual(parts.to_check, [])

    def test_a_thread_the_author_replied_to_is_answered_even_if_the_bot_spoke_last(self) -> None:
        t = rabbit_thread(
            "T1",
            comment(AUTHOR, "Handled upstream in the loader.", "a1"),
            comment(RABBIT, "Thanks for the context!", "r2"),
        )
        parts = outstanding.partition(snapshot([t]), config())
        self.assertEqual([t.node_id for t in parts.to_check], ["T1"])
        self.assertEqual(parts.unanswered, [])

    def test_ours_resolved_and_the_authors_own_notes_are_not_counted(self) -> None:
        ours = thread("T_us", comment(US, "Nit."))
        closed = rabbit_thread("T_closed", resolved=True)
        note = thread("T_note", comment(AUTHOR, "Context for reviewers: this is temporary."))
        self.assertFalse(outstanding.partition(snapshot([ours, closed, note]), config()))

    def test_a_push_that_outdated_the_thread_is_an_answer_to_check(self) -> None:
        parts = outstanding.partition(snapshot([rabbit_thread(outdated=True)]), config())
        self.assertEqual([t.node_id for t in parts.to_check], ["T1"])
        self.assertEqual(parts.unanswered, [])

    def test_a_colleague_counts_the_same_as_a_bot(self) -> None:
        t = thread("T2", comment("bob", "Why not reuse the helper?"))
        self.assertEqual(len(outstanding.partition(snapshot([t]), config()).unanswered), 1)

    def test_the_author_is_matched_without_regard_to_case(self) -> None:
        t = rabbit_thread("T1", comment("Alice", "Done.", "a1"))
        self.assertEqual(len(outstanding.partition(snapshot([t]), config()).to_check), 1)


class TheFingerprint(unittest.TestCase):
    def test_a_reply_landing_changes_it(self) -> None:
        before = outstanding.fingerprint(snapshot([rabbit_thread()]), config())
        after = outstanding.fingerprint(
            snapshot([rabbit_thread("T1", comment(AUTHOR, "Done.", "a1"))]), config()
        )
        self.assertNotEqual(before, after)

    def test_a_thread_being_resolved_changes_it(self) -> None:
        before = outstanding.fingerprint(snapshot([rabbit_thread()]), config())
        after = outstanding.fingerprint(snapshot([rabbit_thread(resolved=True)]), config())
        self.assertNotEqual(before, after)
        self.assertTrue(
            outstanding.hold_changed(
                snapshot([rabbit_thread(resolved=True)]), config(), PRState(hold_fingerprint=before)
            )
        )

    def test_a_thread_going_outdated_changes_it(self) -> None:
        before = outstanding.fingerprint(snapshot([rabbit_thread()]), config())
        after = outstanding.fingerprint(snapshot([rabbit_thread(outdated=True)]), config())
        self.assertNotEqual(before, after)

    def test_nothing_held_means_nothing_changed(self) -> None:
        self.assertFalse(outstanding.hold_changed(snapshot([rabbit_thread()]), config(), PRState()))


class ReadingTheVerdict(unittest.TestCase):
    def test_an_answer_accepted_without_evidence_does_not_count(self) -> None:
        self.assertEqual(outstanding._parse({"verdict": "addressed", "evidence": ""}), "cannot_tell")

    def test_nonsense_is_not_an_answer(self) -> None:
        self.assertEqual(outstanding._parse({"verdict": "lgtm"}), "cannot_tell")

    def test_a_standing_point_needs_no_evidence(self) -> None:
        self.assertEqual(outstanding._parse({"verdict": "not_addressed"}), "not_addressed")


class DescribingTheHold(unittest.TestCase):
    def test_one_unanswered_thread(self) -> None:
        text = outstanding.describe([rabbit_thread()], [])
        self.assertEqual(
            text,
            "1 review thread from other reviewers has no reply from the author yet (`src/App.tsx:12`)",
        )

    def test_several_are_counted_and_the_first_three_named(self) -> None:
        many = [rabbit_thread(f"T{i}", line=i) for i in range(1, 6)]
        text = outstanding.describe(many, [])
        self.assertIn("5 review threads from other reviewers have", text)
        self.assertIn("`src/App.tsx:3`, …", text)
        self.assertNotIn("src/App.tsx:4", text)

    def test_an_answer_that_did_not_settle_it(self) -> None:
        text = outstanding.describe([], [rabbit_thread()])
        self.assertEqual(
            text,
            "what the author replied or pushed for `src/App.tsx:12` does not settle the point raised there",
        )

    def test_an_answer_that_could_not_be_checked_blames_the_check_not_the_author(self) -> None:
        text = outstanding.describe([], [], [rabbit_thread()])
        self.assertIn("could not be checked on this pass", text)
        self.assertIn("retried on the next", text)
        self.assertNotIn("does not settle", text)


class CheckingAnAnswer(WithStore):
    def check(self, snap: PRSnapshot, t: ReviewThread) -> str:
        return outstanding.check_answer(
            thread=t,
            snapshot=snap,
            cfg=config(),
            global_cfg=Globals(),  # type: ignore[arg-type]
            store=self.store,
            checkout_path=None,
            personality_dir=PERSONALITY,
            repo_context=None,
            debug=Quiet(),  # type: ignore[arg-type]
        )

    def test_the_same_reply_against_the_same_code_is_asked_about_once(self) -> None:
        t = rabbit_thread("T1", comment(AUTHOR, "Done.", "a1"))
        with mock.patch("reviewer.outstanding.model.run", return_value=answer("addressed")) as run:
            self.assertEqual(self.check(snapshot([t]), t), "addressed")
            self.assertEqual(self.check(snapshot([t]), t), "addressed")
        self.assertEqual(run.call_count, 1)

    def test_a_new_reply_is_a_new_question(self) -> None:
        first = rabbit_thread("T1", comment(AUTHOR, "Not needed.", "a1"))
        second = rabbit_thread("T1", comment(AUTHOR, "Not needed.", "a1"), comment(AUTHOR, "Fixed after all.", "a2"))
        with mock.patch(
            "reviewer.outstanding.model.run",
            side_effect=[answer("not_addressed"), answer("addressed")],
        ) as run:
            self.assertEqual(self.check(snapshot([first]), first), "not_addressed")
            self.assertEqual(self.check(snapshot([second]), second), "addressed")
        self.assertEqual(run.call_count, 2)

    def test_a_new_commit_is_a_new_question(self) -> None:
        t = rabbit_thread("T1", comment(AUTHOR, "Fixed.", "a1"))
        with mock.patch(
            "reviewer.outstanding.model.run",
            side_effect=[answer("not_addressed"), answer("addressed")],
        ) as run:
            self.check(snapshot([t], head_sha="old"), t)
            self.assertEqual(self.check(snapshot([t], head_sha="new"), t), "addressed")
        self.assertEqual(run.call_count, 2)

    def test_a_failed_call_is_not_remembered(self) -> None:
        t = rabbit_thread("T1", comment(AUTHOR, "Fixed.", "a1"))
        with mock.patch(
            "reviewer.outstanding.model.run",
            side_effect=[ModelError("boom"), answer("addressed")],
        ) as run:
            self.assertEqual(self.check(snapshot([t]), t), "unchecked")
            self.assertEqual(self.check(snapshot([t]), t), "addressed")
        self.assertEqual(run.call_count, 2)

    def test_the_whole_conversation_goes_to_the_model(self) -> None:
        t = rabbit_thread("T1", comment(AUTHOR, "Handled upstream in the loader.", "a1"))
        text = prompt.build_answer_check_prompt(
            cfg=config(), snapshot=snapshot([t]), thread=t, checkout_path=None
        )
        self.assertIn("Guard against a null account.", text)
        self.assertIn("Handled upstream in the loader.", text)
        self.assertIn(f"Author of the pull request: @{AUTHOR}", text)
        self.assertIn("not yours", text)
        self.assertIn("Check the author's reply against the code", text)

    def test_a_push_without_a_reply_is_put_to_the_model_as_such(self) -> None:
        t = rabbit_thread(outdated=True)
        text = prompt.build_answer_check_prompt(
            cfg=config(), snapshot=snapshot([t]), thread=t, checkout_path=None
        )
        self.assertIn("outdated by a push with no reply", text)
        self.assertIn("settled only when the code now does what was asked", text)
        self.assertNotIn("Check the author's reply", text)


class PublishingACleanReview(WithStore):
    def test_with_nobody_elses_threads_open_it_approves(self) -> None:
        posted = self.publish(self.reviewer(), snapshot([]))
        self.assertEqual(posted["event"], "APPROVE")
        self.assertIsNone(self.store.get_pr("acme/widgets", 7).hold_fingerprint)

    def test_an_unanswered_thread_holds_it_without_asking_the_model(self) -> None:
        snap = snapshot([rabbit_thread()])
        with mock.patch("reviewer.outstanding.model.run") as run:
            posted = self.publish(self.reviewer(), snap)
        self.assertEqual(posted["event"], "COMMENT")
        self.assertIn("Not approving yet: 1 review thread from other reviewers", posted["body"])
        self.assertIn("`src/App.tsx:12`", posted["body"])
        self.assertIn("no need to push", posted["body"])
        run.assert_not_called()
        self.assertEqual(
            self.store.get_pr("acme/widgets", 7).hold_fingerprint,
            outstanding.fingerprint(snap, config()),
        )

    def test_a_reply_that_holds_up_clears_the_way(self) -> None:
        snap = snapshot([rabbit_thread("T1", comment(AUTHOR, "Fixed in the loader.", "a1"))])
        with mock.patch("reviewer.outstanding.model.run", return_value=answer("addressed")):
            posted = self.publish(self.reviewer(), snap)
        self.assertEqual(posted["event"], "APPROVE")

    def test_a_reply_that_does_not_hold_up_keeps_the_hold_and_says_where(self) -> None:
        snap = snapshot([rabbit_thread("T1", comment(AUTHOR, "Not needed.", "a1"))])
        with mock.patch("reviewer.outstanding.model.run", return_value=answer("not_addressed")):
            posted = self.publish(self.reviewer(), snap)
        self.assertEqual(posted["event"], "COMMENT")
        self.assertIn("pushed for `src/App.tsx:12` does not settle", posted["body"])

    def test_a_fix_pushed_without_a_reply_clears_the_way_when_the_code_bears_it_out(self) -> None:
        snap = snapshot([rabbit_thread(outdated=True)])
        with mock.patch("reviewer.outstanding.model.run", return_value=answer("addressed")):
            posted = self.publish(self.reviewer(), snap)
        self.assertEqual(posted["event"], "APPROVE")

    def test_a_failed_check_holds_without_blaming_the_author_and_is_retried_every_pass(self) -> None:
        snap = snapshot([rabbit_thread("T1", comment(AUTHOR, "Fixed.", "a1"))])
        reviewer = self.reviewer()
        with mock.patch("reviewer.outstanding.model.run", side_effect=ModelError("boom")):
            posted = self.publish(reviewer, snap)
        self.assertEqual(posted["event"], "COMMENT")
        self.assertIn("could not be checked on this pass", posted["body"])
        self.assertNotIn("does not settle", posted["body"])

        pr_state = self.store.get_pr("acme/widgets", 7)
        self.assertEqual(pr_state.hold_fingerprint, outstanding.UNCHECKED_HOLD)
        self.assertTrue(reviewer._has_new_activity(snap, pr_state, None))

        self.rest.reviews.clear()
        with mock.patch("reviewer.outstanding.model.run", return_value=answer("addressed")):
            self.assertTrue(reviewer._revisit_hold(snap, None, None))
        self.assertEqual(self.rest.reviews[0]["event"], "APPROVE")
        self.assertIsNone(self.store.get_pr("acme/widgets", 7).hold_fingerprint)

    def test_the_wait_can_be_switched_off(self) -> None:
        posted = self.publish(
            self.reviewer(config(wait_for_open_threads=False)), snapshot([rabbit_thread()])
        )
        self.assertEqual(posted["event"], "APPROVE")

    def test_a_manual_hold_comes_first_and_spends_nothing(self) -> None:
        snap = snapshot([rabbit_thread("T1", comment(AUTHOR, "Fixed.", "a1"))])
        with mock.patch("reviewer.outstanding.model.run") as run:
            posted = self.publish(self.reviewer(config(mode="manual")), snap)
        self.assertEqual(posted["event"], "COMMENT")
        self.assertIn("for a human to look at", posted["body"])
        run.assert_not_called()


class LiftingTheHold(WithStore):
    def hold(self) -> Reviewer:
        reviewer = self.reviewer()
        self.publish(reviewer, snapshot([rabbit_thread()]))
        self.rest.reviews.clear()
        return reviewer

    def test_the_thread_being_resolved_counts_as_new_activity(self) -> None:
        reviewer = self.hold()
        pr_state = self.store.get_pr("acme/widgets", 7)
        self.assertFalse(reviewer._has_new_activity(snapshot([rabbit_thread()]), pr_state, None))
        self.assertTrue(
            reviewer._has_new_activity(snapshot([rabbit_thread(resolved=True)]), pr_state, None)
        )

    def test_once_clear_it_approves_the_same_commit_without_a_new_round(self) -> None:
        reviewer = self.hold()
        round_before = self.store.get_pr("acme/widgets", 7).review_round

        lifted = reviewer._revisit_hold(snapshot([rabbit_thread(resolved=True)]), None, None)

        self.assertTrue(lifted)
        self.assertEqual(self.rest.reviews[0]["event"], "APPROVE")
        self.assertIn(render.marker(HEAD, "approval"), self.rest.reviews[0]["body"])
        after = self.store.get_pr("acme/widgets", 7)
        self.assertEqual(after.last_review_action, "APPROVE")
        self.assertEqual(after.review_round, round_before)
        self.assertIsNone(after.hold_fingerprint)

    def test_an_answer_that_holds_up_lifts_it_too(self) -> None:
        reviewer = self.hold()
        snap = snapshot([rabbit_thread("T1", comment(AUTHOR, "Fixed in the loader.", "a1"))])
        with mock.patch("reviewer.outstanding.model.run", return_value=answer("addressed")):
            self.assertTrue(reviewer._revisit_hold(snap, None, None))
        self.assertEqual(self.rest.reviews[0]["event"], "APPROVE")

    def test_still_waiting_posts_nothing_and_remembers_the_new_shape(self) -> None:
        reviewer = self.hold()
        snap = snapshot([rabbit_thread(), rabbit_thread("T2", line=40)])

        self.assertFalse(reviewer._revisit_hold(snap, None, None))

        self.assertEqual(self.rest.reviews, [])
        self.assertEqual(
            self.store.get_pr("acme/widgets", 7).hold_fingerprint,
            outstanding.fingerprint(snap, config()),
        )

    def test_a_base_merge_while_held_does_not_strand_it(self) -> None:
        # The head moved, so the cheap path is off; but the compare shows nothing
        # of the author's, so there is no round either. The hold is looked at
        # from there.
        reviewer = self.hold()
        moved = snapshot([rabbit_thread(resolved=True)], head_sha="after-merge")

        posted = reviewer._full_review(
            snapshot=moved,
            checkout_path=None,
            repo_context=None,
            pr_state_round=2,
            discussion=[],
            since_sha=HEAD,
        )

        self.assertTrue(posted)
        self.assertEqual(self.rest.reviews[0]["event"], "APPROVE")
        self.assertEqual(self.store.get_pr("acme/widgets", 7).last_reviewed_head_sha, "after-merge")

    def test_a_failed_check_on_revisit_keeps_retrying(self) -> None:
        reviewer = self.hold()
        snap = snapshot([rabbit_thread("T1", comment(AUTHOR, "Fixed.", "a1"))])
        with mock.patch("reviewer.outstanding.model.run", side_effect=ModelError("boom")):
            self.assertFalse(reviewer._revisit_hold(snap, None, None))
        self.assertEqual(self.rest.reviews, [])
        pr_state = self.store.get_pr("acme/widgets", 7)
        self.assertEqual(pr_state.hold_fingerprint, outstanding.UNCHECKED_HOLD)
        self.assertTrue(reviewer._has_new_activity(snap, pr_state, None))

    def test_a_manual_rule_that_came_to_apply_while_held_stops_the_approval(self) -> None:
        reviewer = self.reviewer(config(manual_only_when={"pr_has_labels": ["needs-human"]}))
        self.publish(reviewer, snapshot([rabbit_thread()]))
        self.rest.reviews.clear()

        with mock.patch("reviewer.outstanding.model.run") as run, self.assertLogs(
            log.LOGGER_NAME, level="INFO"
        ) as caught:
            lifted = reviewer._revisit_hold(
                snapshot([rabbit_thread(resolved=True)], labels=["needs-human"]), None, None
            )

        self.assertFalse(lifted)
        self.assertEqual(self.rest.reviews, [])
        run.assert_not_called()
        self.assertIsNone(self.store.get_pr("acme/widgets", 7).hold_fingerprint)
        self.assertTrue(any("held: carries label needs-human" in m for m in caught.output))

    def test_the_approval_is_not_posted_twice(self) -> None:
        reviewer = self.hold()
        clear = snapshot([rabbit_thread(resolved=True)])
        reviewer._revisit_hold(clear, None, None)
        self.assertFalse(reviewer._revisit_hold(clear, None, None))
        self.assertEqual(len(self.rest.reviews), 1)


if __name__ == "__main__":
    unittest.main()
