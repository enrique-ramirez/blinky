from __future__ import annotations

import os
import unittest
from typing import Any
from unittest import mock

from reviewer import model, prompt, tracker
from reviewer.config import ConfigError
from reviewer.gh import GitHubError
from reviewer.tracker import ClickUpTracker, GitHubTracker, JiraTracker, Ticket, TrackerError
from tests.test_identity import repo_config


def http(status: int, payload: Any) -> Any:
    def fetch(url: str, headers: dict[str, str]) -> tuple[int, Any]:
        fetch.calls.append((url, headers))
        return status, payload

    fetch.calls = []  # type: ignore[attr-defined]
    return fetch


def github(issue: Any) -> GitHubTracker:
    def get_issue(number: int) -> Any:
        if isinstance(issue, Exception):
            raise issue
        return issue

    return GitHubTracker({}, get_issue=get_issue)


HS_PATTERNS = [r"^\w+\(([0-9a-z]+)\)", r"^\w+/([0-9a-z]+)_"]


class FindingKeys(unittest.TestCase):
    def test_github_closing_keyword_outranks_a_mention(self) -> None:
        keys = github(None).find(
            title="Tidy the header for #3",
            branch="tidy-header",
            body="See #7.\n\nFixes #12",
        )
        self.assertEqual(keys, ["12", "3", "7"])

    def test_the_cap_applies_after_deduplication(self) -> None:
        finder = GitHubTracker({"max_tickets": 2}, get_issue=None)
        self.assertEqual(finder.find(title="#1 #1 #2 #3", branch="", body=""), ["1", "2"])

    def test_clickup_defaults_read_the_forms_clickup_itself_links(self) -> None:
        keys = ClickUpTracker({}).find(
            title="feat: thing CU-86ahzx37j",
            branch="feat/thing",
            body="https://app.clickup.com/t/17tgmdxpr5y and colour #ffffff",
        )
        self.assertEqual(keys, ["17tgmdxpr5y", "86ahzx37j"])

    def test_custom_patterns_read_the_scope_and_the_branch(self) -> None:
        finder = ClickUpTracker({"patterns": HS_PATTERNS})
        self.assertEqual(
            finder.find(
                title="feat(17tgmdxpr5y): run the Workspace in demo mode",
                branch="feat/17tgmdxpr5y_workspace_preview_demo_clients",
                body="",
            ),
            ["17tgmdxpr5y"],
        )
        self.assertEqual(
            finder.find(title="run the Workspace", branch="fix/86ahzx37j_thing", body=""),
            ["86ahzx37j"],
        )

    def test_custom_patterns_replace_the_defaults(self) -> None:
        finder = ClickUpTracker({"patterns": HS_PATTERNS})
        self.assertEqual(finder.find(title="CU-86ahzx37j", branch="", body=""), [])

    def test_jira_default_wants_an_upper_case_key(self) -> None:
        finder = JiraTracker({"base_url": "https://acme.atlassian.net"})
        self.assertEqual(
            finder.find(title="DEV-123 and ABC-9", branch="dev-1", body=""),
            ["DEV-123", "ABC-9"],
        )


class FetchingFromClickUp(unittest.TestCase):
    TASK = {
        "id": "17tgmdxpr5y",
        "name": "Workspace preview for demo clients",
        "markdown_description": "# Goal\n\nShow four demo clients.",
        "description": "Goal Show four demo clients.",
        "status": {"status": "Code Review"},
        "url": "https://app.clickup.com/t/17tgmdxpr5y",
    }

    @mock.patch.dict(os.environ, {"CLICKUP_TOKEN": "pk_secret"})
    def test_a_task_becomes_a_ticket(self) -> None:
        fetch = http(200, self.TASK)
        ticket = ClickUpTracker({}, http=fetch).fetch("17tgmdxpr5y")
        self.assertEqual(
            ticket,
            Ticket(
                label="ClickUp task",
                key="17tgmdxpr5y",
                title="Workspace preview for demo clients",
                body="# Goal\n\nShow four demo clients.",
                url="https://app.clickup.com/t/17tgmdxpr5y",
                status="Code Review",
            ),
        )
        url, headers = fetch.calls[0]
        self.assertIn("/task/17tgmdxpr5y?include_markdown_description=true", url)
        self.assertEqual(headers["Authorization"], "pk_secret")

    @mock.patch.dict(os.environ, {"CLICKUP_TOKEN": "pk_secret"})
    def test_a_missing_task_is_none_and_a_refused_token_is_an_error(self) -> None:
        self.assertIsNone(ClickUpTracker({}, http=http(404, "nope")).fetch("x1"))
        with self.assertRaises(TrackerError) as caught:
            ClickUpTracker({}, http=http(401, "Token invalid")).fetch("x1")
        self.assertIn("CLICKUP_TOKEN", str(caught.exception))
        with self.assertRaises(TrackerError):
            ClickUpTracker({}, http=http(0, "connection refused")).fetch("x1")

    @mock.patch.dict(os.environ, {}, clear=False)
    def test_without_a_token_the_review_runs_without_the_ticket(self) -> None:
        os.environ.pop("CLICKUP_TOKEN", None)
        fetch = http(200, self.TASK)
        with self.assertLogs(level="WARNING") as logs:
            found = ClickUpTracker({}, http=fetch).tickets(
                label="acme/widgets#7", title="CU-86ahzx37j", branch="", body=""
            )
        self.assertEqual(found, [])
        self.assertEqual(fetch.calls, [])
        self.assertIn("CLICKUP_TOKEN is not set", "\n".join(logs.output))

    @mock.patch.dict(os.environ, {"CLICKUP_TOKEN": "pk_secret"})
    def test_a_review_with_no_reference_says_which_tracker_it_looked_with(self) -> None:
        with self.assertLogs(level="INFO") as logs:
            found = ClickUpTracker({}, http=http(200, self.TASK)).tickets(
                label="acme/widgets#7", title="Tidy the header", branch="tidy", body=""
            )
        self.assertEqual(found, [])
        self.assertIn("acme/widgets#7: no ClickUp task referenced", "\n".join(logs.output))

    @mock.patch.dict(os.environ, {"CLICKUP_TOKEN": "pk_secret"})
    def test_the_probe_says_who_the_token_is(self) -> None:
        who = ClickUpTracker({}, http=http(200, {"user": {"username": "enrique"}})).probe()
        self.assertEqual(who, "signed in as enrique")

    @mock.patch.dict(os.environ, {}, clear=False)
    def test_the_probe_names_the_missing_variable(self) -> None:
        os.environ.pop("CLICKUP_TOKEN", None)
        with self.assertRaises(TrackerError) as caught:
            ClickUpTracker({}, http=http(200, {})).probe()
        self.assertEqual(str(caught.exception), "CLICKUP_TOKEN is not set in .env")


class FetchingFromJira(unittest.TestCase):
    ISSUE = {
        "key": "DEV-12",
        "fields": {"summary": "Demo mode", "description": None, "status": {"name": "Done"}},
    }

    @mock.patch.dict(os.environ, {"JIRA_TOKEN": "tok", "JIRA_EMAIL": "me@acme.test"})
    def test_cloud_uses_basic_auth_with_the_email(self) -> None:
        fetch = http(200, self.ISSUE)
        ticket = JiraTracker({"base_url": "https://acme.atlassian.net/"}, http=fetch).fetch(
            "DEV-12"
        )
        self.assertEqual(ticket.key, "DEV-12")
        self.assertEqual(ticket.body, "")
        self.assertEqual(ticket.status, "Done")
        self.assertEqual(ticket.url, "https://acme.atlassian.net/browse/DEV-12")
        url, headers = fetch.calls[0]
        self.assertTrue(url.startswith("https://acme.atlassian.net/rest/api/2/issue/DEV-12"))
        self.assertTrue(headers["Authorization"].startswith("Basic "))

    @mock.patch.dict(os.environ, {"JIRA_TOKEN": "tok"}, clear=False)
    def test_server_uses_the_token_as_a_bearer(self) -> None:
        os.environ.pop("JIRA_EMAIL", None)
        fetch = http(200, self.ISSUE)
        JiraTracker({"base_url": "https://jira.acme.test"}, http=fetch).fetch("DEV-12")
        self.assertEqual(fetch.calls[0][1]["Authorization"], "Bearer tok")


class FetchingFromGitHub(unittest.TestCase):
    def test_an_issue_becomes_a_ticket(self) -> None:
        issue = {"number": 12, "title": "Demo mode", "body": "Four clients.", "state": "open"}
        ticket = github(issue).fetch("12")
        self.assertEqual(ticket.label, "GitHub issue")
        self.assertEqual(ticket.key, "#12")
        self.assertEqual(ticket.body, "Four clients.")

    def test_another_pull_request_is_not_a_ticket(self) -> None:
        self.assertIsNone(github({"number": 12, "title": "x", "pull_request": {}}).fetch("12"))

    def test_a_github_error_is_reported_not_swallowed_silently(self) -> None:
        with self.assertRaises(TrackerError):
            github(GitHubError(500, "boom", "/issues/12")).fetch("12")


class Configuration(unittest.TestCase):
    def test_the_default_is_github_issues(self) -> None:
        self.assertIsInstance(tracker.build(repo_config()), GitHubTracker)

    def test_an_unknown_type_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            repo_config(tracker={"type": "linear"})

    def test_a_pattern_without_one_group_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            repo_config(tracker={"type": "clickup", "patterns": [r"(\w+)-(\d+)"]})
        with self.assertRaises(ConfigError):
            repo_config(tracker={"type": "clickup", "patterns": ["("]})

    def test_a_bad_cap_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            repo_config(tracker={"max_tickets": "three"})

    def test_jira_needs_a_base_url(self) -> None:
        with self.assertRaises(ConfigError):
            repo_config(tracker={"type": "jira"})

    @mock.patch.dict(os.environ, {"CLICKUP_TOKEN": "pk_secret", "MY_CU": "pk_other"})
    def test_the_tracker_token_never_reaches_the_model(self) -> None:
        repo_config(tracker={"type": "clickup"})
        repo_config(tracker={"type": "clickup", "token_env": "MY_CU"})
        env = model._child_env()
        self.assertNotIn("CLICKUP_TOKEN", env)
        self.assertNotIn("MY_CU", env)


class InThePrompt(unittest.TestCase):
    def test_a_ticket_carries_its_label_status_and_link(self) -> None:
        block = prompt._ticket_block(
            [
                Ticket(
                    label="ClickUp task",
                    key="17tgmdxpr5y",
                    title="Demo mode",
                    body="Four clients.",
                    url="https://app.clickup.com/t/17tgmdxpr5y",
                    status="Code Review",
                )
            ]
        )
        self.assertIn("#### ClickUp task 17tgmdxpr5y: Demo mode", block)
        self.assertIn("Code Review · https://app.clickup.com/t/17tgmdxpr5y", block)
        self.assertIn("Four clients.", block)

    def test_no_ticket_says_so(self) -> None:
        self.assertIn("No linked ticket was found", prompt._ticket_block([]))


if __name__ == "__main__":
    unittest.main()
