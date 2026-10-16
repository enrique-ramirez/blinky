from __future__ import annotations

import unittest

from reviewer.preflight import _only_team_reviewers_hidden

REVIEWER_PATH = ["repository", "pullRequest", "reviewRequests", "nodes", 0, "requestedReviewer"]


class TeamReviewerClassification(unittest.TestCase):
    def test_forbidden_team_node_is_not_a_pull_request_denial(self) -> None:
        errors = [
            {
                "type": "FORBIDDEN",
                "path": REVIEWER_PATH,
                "message": "Resource not accessible by personal access token",
            },
            {"type": "FORBIDDEN", "path": [*REVIEWER_PATH[:-2], 1, "requestedReviewer"]},
        ]
        self.assertTrue(_only_team_reviewers_hidden(errors))

    def test_denial_of_the_whole_pull_request_still_counts(self) -> None:
        errors = [
            {
                "type": "FORBIDDEN",
                "path": ["repository", "pullRequest"],
                "message": "Resource not accessible by personal access token",
            }
        ]
        self.assertFalse(_only_team_reviewers_hidden(errors))

    def test_a_mixed_bag_is_a_denial(self) -> None:
        errors = [
            {"type": "FORBIDDEN", "path": REVIEWER_PATH},
            {"type": "NOT_FOUND", "path": ["repository", "pullRequest", "reviewRequests"]},
        ]
        self.assertFalse(_only_team_reviewers_hidden(errors))

    def test_no_errors_is_not_the_team_case(self) -> None:
        self.assertFalse(_only_team_reviewers_hidden([]))


if __name__ == "__main__":
    unittest.main()
