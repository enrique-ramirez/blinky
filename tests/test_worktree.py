from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from reviewer import worktree
from reviewer.worktree import _glob_regex


class ContextGlobs(unittest.TestCase):
    def test_star_stays_in_one_segment(self) -> None:
        rx = _glob_regex(".claude/*.md")
        self.assertTrue(rx.match(".claude/README.md"))
        self.assertFalse(rx.match(".claude/agents/reviewer.md"))

    def test_double_star_crosses_segments(self) -> None:
        rx = _glob_regex(".claude/**/*.md")
        self.assertTrue(rx.match(".claude/agents/reviewer.md"))
        self.assertTrue(rx.match(".claude/README.md"))
        self.assertFalse(rx.match("docs/README.md"))

    def test_bare_name_is_top_level_only(self) -> None:
        rx = _glob_regex("AGENTS.md")
        self.assertTrue(rx.match("AGENTS.md"))
        self.assertFalse(rx.match("packages/web/AGENTS.md"))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo),
        },
    )


class ReadingTrustedFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp())
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "AGENTS.md").write_text("A" * 300)
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "README.md").write_text("R" * 100)
        (self.repo / ".claude" / "agents").mkdir()
        (self.repo / ".claude" / "agents" / "aaa.md").write_text("Z" * 100)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.patterns = ["AGENTS.md", ".claude/*.md"]

    def read(self, max_chars: int) -> worktree.RepoContext:
        return worktree.read_trusted_files(self.repo, "main", self.patterns, max_chars)

    def test_pattern_order_beats_alphabetical(self) -> None:
        context = self.read(10_000)
        self.assertEqual(list(context.files), ["AGENTS.md", ".claude/README.md"])
        self.assertTrue(context.complete)

    def test_subagent_definitions_are_not_swept_in(self) -> None:
        self.assertNotIn(".claude/agents/aaa.md", self.read(10_000).files)

    def test_budget_reports_what_it_cut(self) -> None:
        context = self.read(200)
        self.assertEqual(context.truncated, ["AGENTS.md"])
        self.assertEqual(context.dropped, [".claude/README.md"])
        self.assertTrue(context.files["AGENTS.md"].endswith("[truncated]"))
        self.assertFalse(context.complete)


if __name__ == "__main__":
    unittest.main()
