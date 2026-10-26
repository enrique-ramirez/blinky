from __future__ import annotations

import unittest

from reviewer import prompt
from reviewer.diff import DiffBundle
from reviewer.gh.graphql import CheckState, PRSnapshot, ReviewThread, ThreadComment
from tests.test_identity import repo_config

RABBIT_BODY = (
    "**Consider extracting this into a hook.**\n\n"
    "The component recomputes the list on every render. A long walkthrough "
    "follows, with a diff suggestion and three paragraphs of rationale."
)


def comment(author: str, body: str, node_id: str = "c1") -> ThreadComment:
    return ThreadComment(
        node_id=node_id, database_id=1, author=author, body=body, created_at="", url=""
    )


def thread(node_id: str, *comments: ThreadComment) -> ReviewThread:
    return ReviewThread(
        node_id=node_id,
        is_resolved=False,
        is_outdated=False,
        path="src/App.tsx",
        line=12,
        original_line=12,
        diff_side="RIGHT",
        comments=list(comments),
    )


def snapshot(threads: list[ReviewThread]) -> PRSnapshot:
    return PRSnapshot(
        number=7,
        title="Tidy the header",
        is_draft=False,
        review_decision=None,
        head_sha="abc1234567",
        base_ref="main",
        additions=10,
        deletions=2,
        changed_files=1,
        author="alice",
        labels=[],
        requested_reviewers=[],
        opinionated_reviews=[],
        checks=CheckState(rollup=None),
        threads=threads,
        default_branch="main",
    )


class OtherReviewersInThreads(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = repo_config(review={"other_reviewers": ["coderabbitai[bot]"]})

    def test_their_thread_is_a_headline_with_a_do_not_duplicate_flag(self) -> None:
        t = thread(
            "T1",
            comment("coderabbitai", RABBIT_BODY),
            comment("alice", "Fair, will do.", "c2"),
        )
        block = prompt._thread_block([t], "enrique", self.cfg)
        self.assertIn("thread_id: T1", block)
        self.assertIn("raised by another reviewer, already covered, do not duplicate", block)
        self.assertIn("Consider extracting this into a hook.", block)
        self.assertNotIn("three paragraphs of rationale", block)
        self.assertNotIn("Fair, will do.", block)
        self.assertIn("(1 more in the thread)", block)

    def test_a_colleague_thread_is_sent_whole(self) -> None:
        t = thread("T2", comment("bob", "Why not reuse the existing helper?\n\nSee utils.ts."))
        block = prompt._thread_block([t], "enrique", self.cfg)
        self.assertIn("See utils.ts.", block)
        self.assertNotIn("another reviewer", block)

    def test_bot_suffix_does_not_matter_on_either_side(self) -> None:
        cfg = repo_config(review={"other_reviewers": ["collectors-oppo"]})
        self.assertTrue(prompt.is_other_reviewer("collectors-oppo[bot]", cfg))
        self.assertTrue(prompt.is_other_reviewer("Collectors-Oppo", cfg))
        self.assertFalse(prompt.is_other_reviewer("alice", cfg))

    def test_nothing_changes_when_the_list_is_empty(self) -> None:
        cfg = repo_config()
        t = thread("T3", comment("coderabbitai", RABBIT_BODY))
        block = prompt._thread_block([t], "enrique", cfg)
        self.assertIn("three paragraphs of rationale", block)


class OtherReviewersInConversation(unittest.TestCase):
    def test_their_pr_comments_are_dropped_before_the_cut(self) -> None:
        cfg = repo_config(review={"other_reviewers": ["coderabbitai[bot]"]})
        bots = [
            {"user": {"login": "coderabbitai[bot]"}, "body": f"Walkthrough {i}"}
            for i in range(30)
        ]
        human = {"user": {"login": "bob"}, "body": "Can we land this before Friday?"}
        text = prompt.build_review_user_prompt(
            cfg=cfg,
            snapshot=snapshot([]),
            pr_body="Tidy things.",
            bundle=_empty_bundle(),
            tickets=[],
            discussion=[human, *bots],
            checkout_path=None,
            round_number=1,
            axes=["standards"],
        )
        self.assertIn("Can we land this before Friday?", text)
        self.assertNotIn("Walkthrough", text)


def _empty_bundle() -> DiffBundle:
    return DiffBundle(
        files=[], excluded=[], summarized=[], dropped_for_size=[], truncated=[],
        reviewable_lines=0, total_lines=0,
    )


if __name__ == "__main__":
    unittest.main()
