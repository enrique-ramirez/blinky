from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import log, model, prompt, state
from .config import GlobalConfig, RepoConfig
from .gh.graphql import PRSnapshot, ReviewThread
from .log import DebugSink
from .state import PRState, Store

ADDRESSED = "addressed"
NOT_ADDRESSED = "not_addressed"
CANNOT_TELL = "cannot_tell"
UNCHECKED = "unchecked"

UNCHECKED_HOLD = "check-failed"


@dataclass
class Outstanding:
    unanswered: list[ReviewThread]
    to_check: list[ReviewThread]

    def __bool__(self) -> bool:
        return bool(self.unanswered or self.to_check)


@dataclass
class Hold:
    reason: str
    retry: bool = False

    def __bool__(self) -> bool:
        return bool(self.reason)


def partition(snapshot: PRSnapshot, cfg: RepoConfig) -> Outstanding:
    # Ours are handled elsewhere, and a thread the author opened on their own
    # pull request is a note, not a review comment.
    author = snapshot.author.lower()
    unanswered: list[ReviewThread] = []
    to_check: list[ReviewThread] = []
    for thread in snapshot.threads:
        if thread.is_resolved or thread.is_ours(cfg.identity):
            continue
        if not thread.comments or thread.started_by.lower() == author:
            continue
        # The wait is on the author, not on whoever opened the thread: a bot that
        # opens threads and never resolves them would otherwise hold every approval.
        if thread.replied_by(author) or thread.is_outdated:
            to_check.append(thread)
        else:
            unanswered.append(thread)
    return Outstanding(unanswered, to_check)


def fingerprint(snapshot: PRSnapshot, cfg: RepoConfig) -> str:
    parts = partition(snapshot, cfg)
    pairs = sorted(
        (t.node_id, t.last_comment.node_id if t.last_comment else "", t.is_outdated)
        for t in parts.unanswered + parts.to_check
    )
    return json.dumps(pairs)


def hold_changed(snapshot: PRSnapshot, cfg: RepoConfig, pr_state: PRState) -> bool:
    if pr_state.hold_fingerprint is None:
        return False
    return fingerprint(snapshot, cfg) != pr_state.hold_fingerprint


def _location(thread: ReviewThread) -> str:
    return f"`{thread.path}:{thread.line or thread.original_line or '?'}`"


def _some(threads: list[ReviewThread]) -> str:
    named = ", ".join(_location(t) for t in threads[:3])
    return f"{named}, …" if len(threads) > 3 else named


def describe(
    unanswered: list[ReviewThread],
    unsettled: list[ReviewThread],
    unchecked: list[ReviewThread] | None = None,
) -> str:
    # Named by location, not by who opened them: the author can find a line, and
    # the other reviewers are not this review's subject.
    clauses: list[str] = []
    if unanswered:
        n = len(unanswered)
        clauses.append(
            f"{n} review thread{'s' if n != 1 else ''} from other reviewers "
            f"{'have' if n != 1 else 'has'} no reply from the author yet "
            f"({_some(unanswered)})"
        )
    if unsettled:
        n = len(unsettled)
        clauses.append(
            f"what the author replied or pushed for {_some(unsettled)} does not "
            f"settle the point{'s' if n != 1 else ''} raised there"
        )
    if unchecked:
        n = len(unchecked)
        clauses.append(
            f"the author's answer{'s' if n != 1 else ''} on {_some(unchecked)} could "
            f"not be checked on this pass (the check failed; it is retried on the next)"
        )
    return "; ".join(clauses)


def _parse(payload: dict[str, Any]) -> str:
    verdict = str(payload.get("verdict") or CANNOT_TELL).strip().lower()
    if verdict not in (ADDRESSED, NOT_ADDRESSED, CANNOT_TELL):
        return CANNOT_TELL
    if verdict == ADDRESSED and not str(payload.get("evidence") or "").strip():
        log.get().info("answer accepted without evidence — not counting it")
        return CANNOT_TELL
    return verdict


def check_answer(
    *,
    thread: ReviewThread,
    snapshot: PRSnapshot,
    cfg: RepoConfig,
    global_cfg: GlobalConfig,
    store: Store,
    checkout_path: Path | None,
    personality_dir: Path,
    repo_context: dict[str, str] | None,
    debug: DebugSink,
    spend: model.Spend | None = None,
) -> str:
    last = thread.last_comment
    last_id = last.node_id if last else ""
    cached = store.thread_verdict(cfg.repo, snapshot.number, thread.node_id)
    if cached is not None and cached[1:] == (last_id, snapshot.head_sha):
        return cached[0]

    system = prompt.build_system(personality_dir, cfg, repo_context)
    user = prompt.build_answer_check_prompt(
        cfg=cfg, snapshot=snapshot, thread=thread, checkout_path=checkout_path
    )
    tag = f"answer-{thread.node_id[:8]}"
    debug.write(cfg.repo, snapshot.number, f"{tag}-prompt.md", user)

    try:
        result = model.run(
            global_cfg.thread_provider_for(cfg),
            system_prompt=system,
            user_prompt=user,
            add_dir=checkout_path,
            label=f"{cfg.repo}#{snapshot.number}",
            on_progress=state.heartbeat(store, cfg.repo, snapshot.number),
        )
    except model.ModelError as exc:
        log.get().error(
            "answer check failed on #%s thread %s: %s",
            snapshot.number,
            thread.node_id[:8],
            exc,
        )
        return UNCHECKED

    if spend is not None:
        spend.add(result)
    debug.write(cfg.repo, snapshot.number, f"{tag}-response.json", result.payload)

    verdict = _parse(result.payload)
    store.record_thread_verdict(
        cfg.repo, snapshot.number, thread.node_id, verdict, last_id, snapshot.head_sha
    )
    log.get().info(
        "#%s thread %s: the author's answer is %s",
        snapshot.number,
        thread.node_id[:8],
        verdict.replace("_", " "),
    )
    return verdict


def hold_reason(
    *,
    snapshot: PRSnapshot,
    cfg: RepoConfig,
    global_cfg: GlobalConfig,
    store: Store,
    checkout_path: Path | None,
    personality_dir: Path,
    repo_context: dict[str, str] | None,
    debug: DebugSink,
    spend: model.Spend | None = None,
) -> Hold:
    if not cfg.approval.get("wait_for_open_threads", True):
        return Hold("")
    parts = partition(snapshot, cfg)
    if parts.unanswered:
        return Hold(describe(parts.unanswered, []))

    unsettled: list[ReviewThread] = []
    unchecked: list[ReviewThread] = []
    for t in parts.to_check:
        verdict = check_answer(
            thread=t,
            snapshot=snapshot,
            cfg=cfg,
            global_cfg=global_cfg,
            store=store,
            checkout_path=checkout_path,
            personality_dir=personality_dir,
            repo_context=repo_context,
            debug=debug,
            spend=spend,
        )
        if verdict == UNCHECKED:
            unchecked.append(t)
        elif verdict != ADDRESSED:
            unsettled.append(t)
    return Hold(describe([], unsettled, unchecked), retry=bool(unchecked))
