"""Token preflight: what can this token actually reach?

Fine-grained tokens gate GraphQL fields more tightly than REST, and the failure
mode is a flat "Resource not accessible by personal access token" with no
indication of which permission is missing. This walks the capabilities the
reviewer depends on, one at a time, and maps each failure back to the permission
that grants it.

Run it whenever the token changes:

    ./blinky.sh --check
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import identity, providers, tracker, worktree
from .config import DEFAULT_CONTEXT_PATHS, GlobalConfig, RepoConfig
from .gh import GitHubError
from .gh.rest import ACCEPT, API_VERSION, USER_AGENT

REQUIRED = "required"
OPTIONAL = "optional"


@dataclass
class Probe:
    label: str
    permission: str
    necessity: str
    status: str = "?"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": ACCEPT,
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }


def _rest(token: str, api_url: str, path: str) -> tuple[int, Any]:
    req = urllib.request.Request(f"{api_url}{path}", headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")[:160]
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def _graphql(
    token: str, url: str, inner: str, owner: str, name: str, number: int
) -> tuple[str, list[dict[str, Any]]]:
    """Run one probe query. Returns a transport error, or the GraphQL errors.

    The errors come back whole rather than as a first message, because the
    reviewers probe needs their type and path to tell a missing permission from
    a node the token was never meant to resolve.
    """
    query = (
        "query($o:String!,$n:String!,$num:Int!){repository(owner:$o,name:$n)"
        f"{{pullRequest(number:$num){{{inner}}}}}}}"
    )
    payload = json.dumps(
        {"query": query, "variables": {"o": owner, "n": name, "num": number}}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={**_headers(token), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}", []
    except urllib.error.URLError as exc:
        return str(exc.reason), []

    return "", [e for e in (body.get("errors") or []) if isinstance(e, dict)]


def _only_team_reviewers_hidden(errors: list[dict[str, Any]]) -> bool:
    """Every error is a FORBIDDEN on a requestedReviewer node.

    requestedReviewer is a union of User, Team and Mannequin, and a fine-grained
    token resolves a Team only with the organisation's Members: Read. Without it
    GitHub still returns the request list, with that one node nulled, which is
    exactly how the live snapshot treats it. So this is not a Pull requests
    permission problem, and it only matters when a review is requested of a team
    rather than of you.
    """
    return bool(errors) and all(
        e.get("type") == "FORBIDDEN"
        and (e.get("path") or [None])[-1] == "requestedReviewer"
        for e in errors
    )


def _check_token_source(token: str) -> None:
    from .config import ENV_SOURCES

    source = ENV_SOURCES.get("GITHUB_TOKEN", "unknown")
    print(f"token  : ...{token[-6:]}   (from {source})")
    if "shadowing" in source:
        print("         An exported GITHUB_TOKEN is overriding .env. If you")
        print("         rotated the token, run `unset GITHUB_TOKEN` first.")


def _check_account(global_cfg: GlobalConfig, repos: list[RepoConfig]) -> int:
    """Who the token is, and whether every repo config agrees with that."""
    try:
        login = identity.fetch_login(global_cfg.token, global_cfg.api_url)
    except identity.LookupFailed as exc:
        print(f"account: could not be read — {exc}")
        print("         Without it the reviewer cannot recognise your own pull")
        print("         requests, and skip_own_prs will not fire.")
        return 1

    print(f"account: @{login}")
    wrong = [c for c in repos if c.identity and c.identity.lower() != login.lower()]
    for cfg in wrong:
        print(f'         {cfg.source_file.name} sets "identity": "{cfg.identity}"')
    if wrong:
        print("         Remove the key — it is detected from the token — or")
        print(f"         correct it to {login!r}. Left as it is, the reviewer")
        print("         would work through your own pull requests.")
    return 1 if wrong else 0


def _providers_in_use(
    global_cfg: GlobalConfig, repos: list[RepoConfig]
) -> dict[str, tuple[dict[str, Any], list[str]]]:
    """Every provider a tick could reach for, and who asked for it.

    Not just the default: a per-repository override naming a CLI that was never
    installed should be a line here, in the command whose whole job is finding
    that out, rather than a failed review an hour into a watch loop.
    """
    found: dict[str, tuple[dict[str, Any], list[str]]] = {}
    summaries = global_cfg.merge_summary.get("enabled", True)

    def note(cfg: dict[str, Any], user: str) -> None:
        _, users = found.setdefault(cfg["name"], (cfg, []))
        if user not in users:
            users.append(user)

    default = global_cfg.provider_for()
    note(default, "default")

    def note_if_different(cfg: dict[str, Any], user: str) -> None:
        # Only what departs from the default earns a name. Listing every
        # repository that agrees with it would bury the one that does not.
        if cfg["name"] != default["name"]:
            note(cfg, user)

    if summaries:
        note_if_different(global_cfg.summary_provider_for(), "merge summaries")
    note_if_different(global_cfg.thread_provider_for(), "thread replies")
    for repo in repos:
        note_if_different(global_cfg.provider_for(repo), repo.repo)
        note_if_different(global_cfg.thread_provider_for(repo), repo.repo)
        if summaries:
            note_if_different(global_cfg.summary_provider_for(repo), repo.repo)
    return found


def _check_provider(name: str, cfg: dict[str, Any], users: list[str]) -> int:
    """Is this one CLI installed, and does it run?"""
    adapter = providers.get(str(cfg["type"]))
    command = adapter.command_name(cfg)
    who = ", ".join(users)
    pinned = f", model {cfg['model']}" if cfg.get("model") else ""

    path = shutil.which(command)
    if path is None:
        print(f"model  : {name} — {command!r} is not on PATH   ({who})")
        print(f"         {adapter.install_hint}")
        print(f"         Or point providers.{name}.command in config/global.json at it.")
        return 1

    try:
        result = subprocess.run(
            [command, *adapter.version_args()],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"model  : {name} — {path} could not be run ({exc})")
        return 1

    version = (result.stdout or result.stderr).strip().splitlines()
    label = version[0] if version else "no version reported"
    if result.returncode != 0:
        print(f"model  : {name} — {path} exited {result.returncode}: {label}")
        return 1

    print(f"model  : {name} — {label}{pinned}   ({who})")
    if adapter.caveat:
        print(f"         note: {adapter.caveat}")
    # Signed in is not something --version can answer; the first review will.
    return 0


def _check_models(global_cfg: GlobalConfig, repos: list[RepoConfig]) -> int:
    worst = 0
    for name, (cfg, users) in _providers_in_use(global_cfg, repos).items():
        worst = max(worst, _check_provider(name, cfg, users))
    return worst


def environment(global_cfg: GlobalConfig, repos: list[RepoConfig]) -> int:
    """What is true regardless of which repository is being reviewed."""
    _check_token_source(global_cfg.token)
    return max(_check_account(global_cfg, repos), _check_models(global_cfg, repos))


def _check_repo_context(cfg: RepoConfig, default_branch: str) -> None:
    """Which convention files a review would read, and whether the budget holds.

    A review that reads none of the repository's rulebook still runs and still
    posts, so this is the place to find out, not the review.
    """
    settings = cfg.review.get("repo_context") or {}
    if not settings.get("enabled", True):
        print("context: off (repo_context.enabled is false)")
        return
    if not cfg.local_path:
        print("context: none (no local_path, so AGENTS.md and friends are not read)")
        return

    patterns = settings.get("paths") or DEFAULT_CONTEXT_PATHS
    max_chars = int(settings.get("max_chars") or 60000)
    try:
        ref = worktree.fetch_base_ref(cfg.local_path, default_branch)
        context = worktree.read_trusted_files(cfg.local_path, ref, patterns, max_chars)
    except worktree.GitError as exc:
        print(f"context: could not read from {default_branch} ({exc})")
        return

    if not context and not context.dropped:
        print(f"context: nothing on {default_branch} matches {', '.join(patterns)}")
        return

    total = sum(len(c) for c in context.files.values())
    print(f"context: {len(context.files)} file(s), {total:,} of {max_chars:,} chars, from {default_branch}")
    for path, content in context.files.items():
        tail = "   TRUNCATED" if path in context.truncated else ""
        print(f"         {len(content):>8,}  {path}{tail}")
    for path in context.dropped:
        print(f"         {'dropped':>8}  {path}")
    if not context.complete:
        print("         Raise review.repo_context.max_chars or narrow its paths;")
        print("         whatever is truncated or dropped is not read on review.")


def _check_tracker(global_cfg: GlobalConfig, cfg: RepoConfig, pr_number: int) -> None:
    if "spec" not in (cfg.review.get("axes") or []):
        print("tracker: unused (the spec axis is off)")
        return

    def get_issue(number: int) -> dict[str, Any] | None:
        path = f"/repos/{cfg.repo}/issues/{number}"
        status, body = _rest(global_cfg.token, global_cfg.api_url, path)
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict):
            raise GitHubError(status, str(body), path)
        return body

    finder = tracker.build(cfg, get_issue=get_issue)
    try:
        who = finder.probe()
    except tracker.TrackerError as exc:
        print(f"tracker: {finder.label} · {exc}, so no ticket is read")
        return
    token_note = f" · {finder.token_var} from .env" if finder.token_var else ""
    print(f"tracker: {finder.label}{token_note} · {who}")

    status, pull = _rest(
        global_cfg.token, global_cfg.api_url, f"/repos/{cfg.repo}/pulls/{pr_number}"
    )
    if status != 200 or not isinstance(pull, dict):
        return
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    try:
        resolved = finder.resolve(
            title=str(pull.get("title") or ""),
            branch=str((head or {}).get("ref") or ""),
            body=str(pull.get("body") or ""),
        )
    except tracker.TrackerError as exc:
        print(f"         PR #{pr_number}: {exc}")
        return
    if not resolved:
        print(
            f"         PR #{pr_number}: no {finder.label} referenced in the "
            "title, branch or description"
        )
    for item in resolved:
        if item.ticket is None:
            print(f"         PR #{pr_number} -> {item.key}: {item.note}")
            continue
        state = f" ({item.ticket.status})" if item.ticket.status else ""
        print(f'         PR #{pr_number} -> {item.ticket.key} "{item.ticket.title[:60]}"{state}')


def run(global_cfg: GlobalConfig, cfg: RepoConfig, pr_number: int | None) -> int:
    """Probe every capability. Returns a process exit code."""
    print(f"repo   : {cfg.repo}")

    status, body = _rest(global_cfg.token, global_cfg.api_url, f"/repos/{cfg.repo}")
    if status == 401:
        print("\n  401 — the token is invalid, revoked, or not in .env yet.")
        return 1
    if status == 403:
        print("\n  403 — the token exists but has no access to this repository.")
        print("        If it was created with 'request access', an organisation")
        print("        owner still needs to approve it.")
        return 1
    if status == 404:
        print("\n  404 — the token cannot see this repository. Check that it was")
        print("        selected under 'Repository access'.")
        return 1
    if status != 200:
        print(f"\n  unexpected {status}: {body}")
        return 1
    default_branch = str(body.get("default_branch") or "main")
    print(f"default: {default_branch}")
    _check_repo_context(cfg, default_branch)

    # Find a PR to probe against; every GraphQL check needs a real one.
    if pr_number is None:
        st, pulls = _rest(
            global_cfg.token, global_cfg.api_url, f"/repos/{cfg.repo}/pulls?state=all&per_page=1"
        )
        if st == 200 and isinstance(pulls, list) and pulls:
            pr_number = pulls[0].get("number")
    if pr_number is None:
        print("\n  no pull request found to probe against; open one or pass --pr N")
        return 1
    print(f"probing: PR #{pr_number}")
    _check_tracker(global_cfg, cfg, pr_number)
    print()

    probes = [
        Probe("read pull requests", "Pull requests: Read", REQUIRED),
        Probe("read PR labels + author", "Pull requests: Read", REQUIRED),
        Probe("read review threads", "Pull requests: Read", REQUIRED),
        Probe("read requested reviewers", "Pull requests: Read", REQUIRED),
        Probe("read CI check rollup", "Checks: Read + Commit statuses: Read", REQUIRED),
        Probe("read file contents", "Contents: Read", OPTIONAL),
    ]
    queries = [
        "number title isDraft headRefOid additions deletions",
        "author{login} labels(first:5){nodes{name}} reviewDecision",
        "reviewThreads(first:5){nodes{id isResolved isOutdated}}",
        "reviewRequests(first:5){nodes{requestedReviewer{__typename}}}",
        "commits(last:1){nodes{commit{statusCheckRollup{state contexts(first:5)"
        "{nodes{__typename ... on CheckRun{name conclusion}}}}}}}",
        None,
    ]

    for probe, inner in zip(probes, queries):
        if inner is None:
            st, _ = _rest(
                global_cfg.token, global_cfg.api_url, f"/repos/{cfg.repo}/contents/README.md"
            )
            probe.status = "ok" if st == 200 else "denied"
            probe.detail = "" if st == 200 else f"HTTP {st}"
            continue
        transport, errors = _graphql(
            global_cfg.token,
            global_cfg.graphql_url,
            inner,
            cfg.owner,
            cfg.name,
            pr_number,
        )
        if transport:
            probe.status, probe.detail = "denied", transport
        elif probe.label == "read requested reviewers" and _only_team_reviewers_hidden(errors):
            probe.status = "denied"
            probe.permission = "Organization -> Members: Read (team reviewers)"
            probe.necessity = OPTIONAL
            probe.detail = (
                "review requests made to a team are invisible; a request made "
                "to you by name still counts"
            )
        elif errors:
            probe.status, probe.detail = "denied", (errors[0].get("message") or "")[:70]
        else:
            probe.status = "ok"

    width = max(len(p.label) for p in probes)
    for probe in probes:
        mark = "ok    " if probe.ok else "DENIED"
        extra = "" if probe.necessity == REQUIRED else "   (optional)"
        print(f"  {mark}  {probe.label:{width}}   {probe.permission}{extra}")
        if not probe.ok and probe.detail:
            print(f"          {'':{width}}   -> {probe.detail}")

    ci_probe = next(p for p in probes if "rollup" in p.label)

    # If the rollup is refused, work out whether anything else can tell us
    # whether CI is green, and how much of CI it actually covers.
    ci_source = "rollup" if ci_probe.ok else None
    if not ci_probe.ok:
        print()
        print("  CI rollup refused. Testing fallbacks:")

        st, head = _rest(
            global_cfg.token, global_cfg.api_url, f"/repos/{cfg.repo}/pulls/{pr_number}"
        )
        sha = head.get("head", {}).get("sha", "") if isinstance(head, dict) else ""

        if sha:
            st, body = _rest(
                global_cfg.token, global_cfg.api_url, f"/repos/{cfg.repo}/commits/{sha}/check-runs"
            )
            n = body.get("total_count", 0) if st == 200 and isinstance(body, dict) else 0
            print(f"    REST /check-runs        HTTP {st}   {n} run(s)")
            if st == 200 and n:
                ci_source = "check_runs"

            st, body = _rest(
                global_cfg.token,
                global_cfg.api_url,
                f"/repos/{cfg.repo}/actions/runs?head_sha={sha}&per_page=100",
            )
            if st == 200 and isinstance(body, dict):
                runs = body.get("workflow_runs") or []
                latest: dict[Any, dict[str, Any]] = {}
                for run in runs:
                    key = run.get("workflow_id") or run.get("name")
                    prev = latest.get(key)
                    if prev is None or str(run.get("created_at") or "") > str(
                        prev.get("created_at") or ""
                    ):
                        latest[key] = run
                print(
                    f"    REST /actions/runs      HTTP {st}   "
                    f"{len(latest)} workflow(s), {len(runs)} run(s) incl. re-runs"
                )
                for run in list(latest.values())[:8]:
                    print(
                        f"        - {run.get('name')} = "
                        f"{run.get('conclusion') or run.get('status')}"
                    )
                if latest and ci_source is None:
                    ci_source = "actions_runs"
            else:
                print(f"    REST /actions/runs      HTTP {st}   (needs Actions: Read)")

            st, body = _rest(
                global_cfg.token, global_cfg.api_url, f"/repos/{cfg.repo}/commits/{sha}/status"
            )
            if st == 200 and isinstance(body, dict):
                statuses = body.get("statuses") or []
                print(
                    f"    REST /status            HTTP {st}   "
                    f"{len(statuses)} status(es), state={body.get('state')}"
                )
                if statuses and ci_source is None:
                    ci_source = "commit_statuses"
                if statuses:
                    for s in statuses[:6]:
                        print(f"        - {s.get('context')} = {s.get('state')}")
            else:
                print(f"    REST /status            HTTP {st}")

    missing_required = [
        p for p in probes if not p.ok and p.necessity == REQUIRED and p is not ci_probe
    ]

    print()
    if not missing_required and ci_source == "rollup":
        print("  Everything the reviewer needs is reachable.")
        return 0

    if missing_required:
        print("  Missing permissions:")
        for permission in sorted({p.permission for p in missing_required}):
            print(f"    - {permission}")
        print()

    if ci_source == "rollup":
        pass
    elif ci_source == "check_runs":
        print("  CI is readable via REST /check-runs. Set in the repo config:")
        print('      "gates": { "ci_source": "check_runs" }')
    elif ci_source == "actions_runs":
        print("  CI is readable via the Actions API. Set in the repo config:")
        print('      "gates": { "ci_source": "actions_runs" }')
        print()
        print("  This reads workflow-run conclusions rather than check runs, so it")
        print("  covers everything running in GitHub Actions. A check posted by a")
        print("  third-party App would not appear — compare the workflow count")
        print("  above against what the PR page shows before relying on it.")
    elif ci_source == "commit_statuses":
        print("  CI is readable via commit statuses only.")
        print("  Those cover integrations that post statuses; they do NOT cover")
        print("  GitHub Actions check runs, so anything running in Actions would")
        print("  go unchecked. Compare the count above against what the PR page")
        print("  shows before relying on it. To use it:")
        print('      "gates": { "ci_source": "commit_statuses" }')
    else:
        print("  No CI signal is reachable with this token.")
        print("  Check runs need the 'Checks' permission, which GitHub offers on")
        print("  GitHub Apps and not on fine-grained tokens. Two ways forward:")
        print()
        print("    1. Drop the CI gate. In")
        print(f"       config/repos/{cfg.source_file.name}, set")
        print('         "gates": { "require_ci_green": false }')
        print("       The reviewer will then review PRs whose builds are red.")
        print()
        print("    2. Use a classic PAT with the 'repo' scope, which can read")
        print("       check runs. It also grants write access to code, so it")
        print("       gives up the guarantee that this tool cannot push.")

    return 0 if (ci_source and not missing_required) else 1
