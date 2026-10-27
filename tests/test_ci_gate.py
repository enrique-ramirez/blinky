"""How CI results reduce to a single go / wait / stop answer.

The case that matters most is a workflow run that died before its first job.
GitHub never turns it into a check run, so the pull request page does not show
it and the merge button does not wait for it; the Actions runs list does show
it. The reviewer has to agree with the page, not with the list.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from reviewer import gates
from reviewer.gh.graphql import CheckState
from tests.test_rounds import repo_config


def _run(name: str, conclusion: str | None, status: str = "COMPLETED") -> dict[str, Any]:
    return {"kind": "actions_run", "name": name, "conclusion": conclusion, "status": status}


def _snapshot(*contexts: dict[str, Any]) -> Any:
    return SimpleNamespace(checks=CheckState(rollup=None, contexts=list(contexts)))


class StartupFailures(unittest.TestCase):
    def test_a_run_that_never_started_does_not_fail_a_green_commit(self) -> None:
        snapshot = _snapshot(
            _run("Test", "SUCCESS"),
            _run("Lint", "SUCCESS"),
            _run("Format", "STARTUP_FAILURE"),
            _run("Build & Preview", "STARTUP_FAILURE"),
        )
        self.assertEqual(gates.ci_status(snapshot, repo_config()), ("green", []))

    def test_a_real_failure_still_blocks_and_is_named_alone(self) -> None:
        snapshot = _snapshot(_run("Test", "FAILURE"), _run("Format", "STARTUP_FAILURE"))
        self.assertEqual(
            gates.ci_status(snapshot, repo_config()), ("failing", ["Test=FAILURE"])
        )

    def test_only_dead_runs_reads_like_a_commit_with_no_ci(self) -> None:
        snapshot = _snapshot(_run("Test", "STARTUP_FAILURE"), _run("Lint", "STARTUP_FAILURE"))
        self.assertEqual(gates.ci_status(snapshot, repo_config()), ("green", []))

    def test_a_run_still_going_is_waited_for(self) -> None:
        snapshot = _snapshot(
            _run("Test", None, status="IN_PROGRESS"), _run("Format", "STARTUP_FAILURE")
        )
        self.assertEqual(gates.ci_status(snapshot, repo_config()), ("pending", ["Test"]))


if __name__ == "__main__":
    unittest.main()
