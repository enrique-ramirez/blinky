"""Where the ticket lives.

Which tracker holds the ticket is set per repo in its ``tracker`` block; the
default is GitHub issues.

The script fetches the ticket, not the model. The model has no network and no
tokens, and that stays true here: the ticket arrives in the prompt as text, the
same way the diff does. Letting the model call the tracker's MCP server instead
would tie the lookup to one CLI signed in to one server, and nothing in the log
could say whether it had bothered.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from . import log
from .gh import GitHubError

USER_AGENT = "blinky/0.1 (+https://github.com)"
MAX_BODY_CHARS = 20000
DEFAULT_MAX_TICKETS = 3

#: Names of environment variables that hold a tracker credential. ``check``
#: fills this in as each repo config loads, and ``model`` strips these names
#: from the child environment, so config must load before any model call.
SECRET_ENV: set[str] = set()

Fetch = Callable[[str, dict[str, str]], tuple[int, Any]]
GetIssue = Callable[[int], "dict[str, Any] | None"]


class TrackerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Ticket:
    label: str
    key: str
    title: str
    body: str
    url: str = ""
    status: str = ""


@dataclass(frozen=True)
class Resolution:
    key: str
    ticket: Ticket | None = None
    note: str = ""
    failed: bool = False


def _http_get(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT, **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")[:300]
    except OSError as exc:
        # URLError and a socket timeout inside read() both land here.
        return 0, str(getattr(exc, "reason", exc))
    try:
        return resp.status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return resp.status, raw[:300]


def _clip(text: Any) -> str:
    body = str(text or "").strip()
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[:MAX_BODY_CHARS] + "\n\n[… ticket body truncated]"


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    compiled = []
    for raw in patterns:
        try:
            pattern = re.compile(raw)
        except re.error as exc:
            raise TrackerError(f"tracker pattern {raw!r} does not compile: {exc}") from exc
        if pattern.groups != 1:
            raise TrackerError(
                f"tracker pattern {raw!r} needs exactly one capture group, the "
                f"ticket id; it has {pattern.groups}"
            )
        compiled.append(pattern)
    return compiled


class Tracker:
    kind = ""
    label = ""
    default_patterns: tuple[str, ...] = ()
    token_env = ""

    def __init__(self, settings: dict[str, Any], *, http: Fetch = _http_get) -> None:
        self.settings = settings
        self.http = http
        self.patterns = _compile(
            [str(p) for p in (settings.get("patterns") or self.default_patterns)]
        )
        cap = settings.get("max_tickets")
        try:
            self.max_tickets = DEFAULT_MAX_TICKETS if cap is None else int(cap)
        except (TypeError, ValueError) as exc:
            raise TrackerError("tracker.max_tickets must be a number") from exc
        if self.max_tickets < 1:
            raise TrackerError("tracker.max_tickets must be at least 1")
        self.token_var = str(settings.get("token_env") or self.token_env)

    @property
    def token(self) -> str:
        return os.environ.get(self.token_var, "").strip() if self.token_var else ""

    @property
    def secret_vars(self) -> set[str]:
        return {self.token_var} if self.token_var else set()

    def find(self, *, title: str, branch: str, body: str) -> list[str]:
        keys: list[str] = []
        for pattern in self.patterns:
            for text in (title or "", branch or "", body or ""):
                for match in pattern.finditer(text):
                    key = match.group(1)
                    if key not in keys:
                        keys.append(key)
        return keys[: self.max_tickets]

    def fetch(self, key: str) -> Ticket | None:
        raise NotImplementedError

    def probe(self) -> str:
        raise NotImplementedError

    def resolve(self, *, title: str, branch: str, body: str) -> list[Resolution]:
        keys = self.find(title=title, branch=branch, body=body)
        if keys and self.token_var and not self.token:
            raise TrackerError(
                f"{', '.join(keys)} referenced, but {self.token_var} is not set in .env"
            )
        resolved: list[Resolution] = []
        for key in keys:
            try:
                ticket = self.fetch(key)
            except TrackerError as exc:
                resolved.append(Resolution(key, note=str(exc), failed=True))
                continue
            if ticket is None:
                resolved.append(
                    Resolution(key, note="not found: a stray reference, or a token that cannot see it")
                )
                continue
            resolved.append(Resolution(key, ticket=ticket))
        return resolved

    def tickets(self, *, label: str, title: str, branch: str, body: str) -> list[Ticket]:
        logger = log.get()
        try:
            resolved = self.resolve(title=title, branch=branch, body=body)
        except TrackerError as exc:
            logger.warning("%s: %s, so the review runs without the ticket", label, exc)
            return []
        if not resolved:
            if self.kind != NoTracker.kind:
                logger.info(
                    "%s: no %s referenced in the title, branch or description",
                    label,
                    self.label,
                )
            return []
        for item in resolved:
            if item.ticket is None:
                say = logger.warning if item.failed else logger.info
                say("%s: %s %s %s", label, self.label, item.key, item.note)
        found = [item.ticket for item in resolved if item.ticket is not None]
        if found:
            logger.info(
                "%s: judging against %s %s",
                label,
                self.label,
                ", ".join(t.key for t in found),
            )
        return found

    def _get(self, url: str, headers: dict[str, str]) -> Any:
        if self.token_var and not self.token:
            raise TrackerError(f"{self.token_var} is not set in .env")
        status, payload = self.http(url, headers)
        if status == 404:
            return None
        if status in (401, 403):
            raise TrackerError(
                f"{self.token_var} was refused (HTTP {status}); check the token in .env"
            )
        if status == 0:
            raise TrackerError(f"could not reach {url}: {payload}")
        if status != 200 or not isinstance(payload, dict):
            raise TrackerError(f"unexpected HTTP {status} from {url}: {str(payload)[:120]}")
        return payload


class NoTracker(Tracker):
    kind = "none"
    label = "ticket"

    def find(self, **_: str) -> list[str]:
        return []

    def fetch(self, key: str) -> Ticket | None:
        return None

    def probe(self) -> str:
        return "no tracker; the spec axis judges against the description"


class GitHubTracker(Tracker):
    kind = "github"
    label = "GitHub issue"
    default_patterns = (
        r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d{1,6})\b",
        r"(?:^|\s)#(\d{1,6})\b",
    )

    def __init__(self, settings: dict[str, Any], *, get_issue: GetIssue | None) -> None:
        super().__init__(settings)
        self.get_issue = get_issue

    def fetch(self, key: str) -> Ticket | None:
        if self.get_issue is None:
            raise TrackerError("no GitHub client to read issues with")
        try:
            issue = self.get_issue(int(key))
        except GitHubError as exc:
            raise TrackerError(f"could not read issue #{key}: {exc}") from exc
        # The issues endpoint also returns pull requests, marked by pull_request.
        if not issue or "pull_request" in issue:
            return None
        return Ticket(
            label=self.label,
            key=f"#{issue.get('number', key)}",
            title=str(issue.get("title") or ""),
            body=_clip(issue.get("body")),
            url=str(issue.get("html_url") or ""),
            status=str(issue.get("state") or ""),
        )

    def probe(self) -> str:
        return "issues on the repository, read with the GitHub token"


class ClickUpTracker(Tracker):
    kind = "clickup"
    label = "ClickUp task"
    token_env = "CLICKUP_TOKEN"
    api = "https://api.clickup.com/api/v2"
    # The forms ClickUp's own GitHub integration recognises.
    default_patterns = (
        r"app\.clickup\.com/t/([0-9a-z]+)\b",
        r"\bCU-(?=[0-9a-z]*\d)([0-9a-z]{6,12})\b",
        r"#(?=[0-9a-z]*\d)([0-9a-z]{6,12})\b",
    )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.token}

    def fetch(self, key: str) -> Ticket | None:
        task = self._get(
            f"{self.api}/task/{key}?include_markdown_description=true", self._headers()
        )
        if task is None:
            return None
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        return Ticket(
            label=self.label,
            key=str(task.get("id") or key),
            title=str(task.get("name") or ""),
            body=_clip(
                task.get("markdown_description")
                or task.get("description")
                or task.get("text_content")
            ),
            url=str(task.get("url") or f"https://app.clickup.com/t/{key}"),
            status=str((status or {}).get("status") or ""),
        )

    def probe(self) -> str:
        me = self._get(f"{self.api}/user", self._headers())
        user = me.get("user") if isinstance(me, dict) else None
        if not isinstance(user, dict):
            raise TrackerError("the token works, but /user returned nothing recognisable")
        return f"signed in as {user.get('username') or user.get('email') or '?'}"


class JiraTracker(Tracker):
    kind = "jira"
    label = "Jira issue"
    token_env = "JIRA_TOKEN"
    email_env = "JIRA_EMAIL"
    default_patterns = (r"\b([A-Z][A-Z0-9_]+-\d+)\b",)

    def __init__(self, settings: dict[str, Any], *, http: Fetch = _http_get) -> None:
        super().__init__(settings, http=http)
        self.base_url = str(settings.get("base_url") or "").rstrip("/")
        self.email_var = str(settings.get("email_env") or self.email_env)

    @property
    def secret_vars(self) -> set[str]:
        return super().secret_vars | {self.email_var}

    def _headers(self) -> dict[str, str]:
        email = os.environ.get(self.email_var, "").strip()
        # Cloud takes Basic with email:token; Server and Data Center take a bearer PAT.
        if email:
            raw = base64.b64encode(f"{email}:{self.token}".encode()).decode()
            return {"Authorization": f"Basic {raw}"}
        return {"Authorization": f"Bearer {self.token}"}

    def fetch(self, key: str) -> Ticket | None:
        # v2, not v3: v3 returns the description as a document tree, not text.
        issue = self._get(
            f"{self.base_url}/rest/api/2/issue/{key}?fields=summary,description,status",
            self._headers(),
        )
        if issue is None:
            return None
        fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
        return Ticket(
            label=self.label,
            key=str(issue.get("key") or key),
            title=str(fields.get("summary") or ""),
            body=_clip(fields.get("description")),
            url=f"{self.base_url}/browse/{issue.get('key') or key}",
            status=str((status or {}).get("name") or ""),
        )

    def probe(self) -> str:
        me = self._get(f"{self.base_url}/rest/api/2/myself", self._headers())
        if not isinstance(me, dict):
            raise TrackerError("the token works, but /myself returned nothing recognisable")
        return f"signed in as {me.get('displayName') or me.get('emailAddress') or '?'}"


TYPES: dict[str, type[Tracker]] = {
    NoTracker.kind: NoTracker,
    GitHubTracker.kind: GitHubTracker,
    ClickUpTracker.kind: ClickUpTracker,
    JiraTracker.kind: JiraTracker,
}


def _kind(settings: dict[str, Any]) -> str:
    return str(settings.get("type") or GitHubTracker.kind)


def _make(
    settings: dict[str, Any],
    *,
    get_issue: GetIssue | None = None,
    http: Fetch | None = None,
) -> Tracker:
    kind = _kind(settings)
    if kind == GitHubTracker.kind:
        return GitHubTracker(settings, get_issue=get_issue)
    return TYPES[kind](settings, http=http) if http else TYPES[kind](settings)


def check(settings: dict[str, Any]) -> None:
    kind = _kind(settings)
    if kind not in TYPES:
        raise TrackerError(f'tracker.type {kind!r} is not one of {", ".join(TYPES)}')
    if not isinstance(settings.get("patterns") or [], list):
        raise TrackerError("tracker.patterns must be a list of regular expressions")
    if kind == JiraTracker.kind and not settings.get("base_url"):
        raise TrackerError(
            'tracker.type "jira" needs "base_url", e.g. "https://acme.atlassian.net"'
        )
    SECRET_ENV.update(_make(settings).secret_vars)


def build(cfg: Any, *, get_issue: GetIssue | None = None, http: Fetch | None = None) -> Tracker:
    return _make(dict(cfg.tracker or {}), get_issue=get_issue, http=http)
