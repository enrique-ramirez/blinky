#!/usr/bin/env python3
"""A progress page for one /build, read from the files the coordinator already writes.

    build-dashboard.py <bundle-dir> [--port <n>] [--once <file.html>]

Reads `build-metrics.md`, `tasks.md` and `spec.md` in the bundle every few seconds and
serves one page on 127.0.0.1. The port comes from the bundle's path, so two builds in two
repositories get two pages; `--port` overrides it. A second start against a bundle that
is already served prints the URL and exits. `--once` writes the page to a file and exits.

The times in the metrics file are the coordinator's own clock. The page also records
when it first saw each row open and close, under `_tmp/<n>-build/dashboard-state.json`,
so "running for" comes from this machine's clock even when the coordinator's is wrong.

The "files changed" stat is the newest write anywhere in the repository's working tree,
listed through `git ls-files` so ignored directories do not count. Without git it falls
back to the bundle's own files.
"""

from __future__ import annotations

import argparse
import html
import http.server
import json
import pathlib
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field

sys.dont_write_bytecode = True

POLL_SECONDS = 3
RECENT_ROWS = 12
QUIET_AMBER_MINUTES = 15
QUIET_RED_MINUTES = 60
CLOCK_DRIFT_MINUTES = 15

OK_WORDS = ("accepted", "passed")
BOUNCE_WORDS = ("bounced",)
DEAD_WORDS = ("died", "stopped")

GROUP_ID = re.compile(r"\bG(\d+)\b")
TASK_ID = re.compile(r"\bT(\d+)[a-z]?\b")
TASK_LINE = re.compile(r"^- \[([ xX])\] (?:T(\d+)\b)?\s*(.*)$")
CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
LAYOUT_GROUP = re.compile(r"\bG(\d+)\s*\(([^)]*)\)")
BACKTICK = re.compile(r"`([^`]+)`")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
LEADING_DURATION = re.compile(r"^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s)?")


@dataclass
class Task:
    number: int
    done: bool
    title: str


@dataclass
class Group:
    number: int
    lane: str
    tasks: list[int]
    label: str


@dataclass
class Lane:
    key: str
    name: str
    groups: list[Group]


@dataclass
class Row:
    number: int
    what: str
    kind: str
    lane: str
    new: str
    start: str
    end: str
    wall: str
    tokens: str
    tools: str
    outcome: str
    groups: list[int] = field(default_factory=list)
    tasks: list[int] = field(default_factory=list)

    @property
    def open(self) -> bool:
        return not self.end and not self.outcome


@dataclass
class Bundle:
    path: pathlib.Path
    title: str
    compared: str
    elapsed_line: str
    tasks: list[Task]
    cleanup: list[Task]
    lanes: list[Lane]
    rows: list[Row]
    changed: float
    written: float


# --- reading the bundle -------------------------------------------------------------


def section(text: str, heading: str) -> str:
    """The body of a `## heading`, up to the next `## `."""
    lines = text.split("\n")
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().lower().startswith(heading.lower())
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def table_rows(body: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in body.split("\n"):
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or all(not cell for cell in cells):
            continue
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        rows.append(cells)
    return rows


def first_line_after(text: str, prefix: str) -> str:
    for line in text.split("\n"):
        if line.startswith(prefix):
            return HTML_COMMENT.sub("", line[len(prefix):]).strip()
    return ""


def read_tasks(text: str) -> tuple[list[Task], list[Task]]:
    tasks: list[Task] = []
    cleanup: list[Task] = []
    in_cleanup = False
    for line in text.split("\n"):
        if line.startswith("## "):
            in_cleanup = line[3:].strip().lower().startswith("cleanup")
            continue
        match = TASK_LINE.match(line)
        if not match:
            continue
        done = match.group(1).lower() == "x"
        body = match.group(3).strip()
        title = re.split(r"(?<=[^.])\.(?:\s|$)|\s\|\s", body, maxsplit=1)[0].strip()
        if match.group(2) and not in_cleanup:
            tasks.append(Task(int(match.group(2)), done, title))
        else:
            cleanup.append(Task(len(cleanup) + 1, done, title))
    return tasks, cleanup


def group_label(inner: str, titles: dict[int, str]) -> str:
    text = inner.strip()
    only_id = re.fullmatch(r"T(\d+)", text)
    if only_id:
        return titles.get(int(only_id.group(1)), text)
    stripped = re.sub(r"^T(\d+)(?!'s)\s*[,:]?\s+", "", text)
    for cut in (", after ", ", builder only", ", written first"):
        if cut in stripped:
            stripped = stripped.split(cut, 1)[0]
    stripped = stripped.strip()
    first_task = TASK_ID.search(text)
    if not stripped or stripped.startswith(("after ", "builder only")):
        return titles.get(int(first_task.group(1)), text) if first_task else text
    return stripped


def read_layout(body: str, titles: dict[int, str]) -> list[Lane]:
    lanes: list[Lane] = []
    for cells in table_rows(body):
        if len(cells) < 2 or cells[0].lower() == "lane":
            continue
        head = cells[0]
        key, _, name = head.partition(",")
        key = key.strip()
        if " " in key:
            key, name = key.split(" ", 1)[0], head
        groups = [
            Group(int(number), key, [int(t) for t in TASK_ID.findall(inner)], group_label(inner, titles))
            for number, inner in LAYOUT_GROUP.findall(cells[1])
        ]
        lanes.append(Lane(key, name.strip() or key, groups))
    return lanes


def read_rows(body: str, lanes: list[Lane]) -> list[Row]:
    by_task: dict[tuple[str, int], int] = {}
    any_lane: dict[int, int] = {}
    for lane in lanes:
        for group in lane.groups:
            for task in group.tasks:
                by_task[(lane.key, task)] = group.number
                any_lane.setdefault(task, group.number)
    rows: list[Row] = []
    for cells in table_rows(body):
        if cells[0] in ("#", "") or not cells[0].isdigit():
            continue
        cells = (cells + [""] * 11)[:11]
        row = Row(int(cells[0]), *cells[1:])
        row.tasks = [int(t) for t in TASK_ID.findall(row.what)]
        row.groups = [int(g) for g in GROUP_ID.findall(row.what)]
        for task in row.tasks:
            number = by_task.get((row.lane, task), any_lane.get(task))
            if number is not None and number not in row.groups:
                row.groups.append(number)
        rows.append(row)
    return rows


def tree_changed(root: pathlib.Path) -> float:
    """The newest write among the files git tracks or would track under `root`; 0.0 without git."""
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0.0
    newest = 0.0
    for name in listing.split(b"\0"):
        if not name:
            continue
        try:
            newest = max(newest, (root / name.decode("utf-8", errors="surrogateescape")).stat().st_mtime)
        except OSError:
            continue
    return newest


def read_bundle(path: pathlib.Path) -> Bundle:
    metrics_path = path / "build-metrics.md"
    tasks_path = path / "tasks.md"
    spec_path = path / "spec.md"
    metrics = metrics_path.read_text(encoding="utf-8") if metrics_path.exists() else ""
    tasks_text = tasks_path.read_text(encoding="utf-8") if tasks_path.exists() else ""
    spec = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""

    title = next((line[2:].strip() for line in spec.split("\n") if line.startswith("# ")), path.name)
    tasks, cleanup = read_tasks(tasks_text)
    titles = {task.number: task.title for task in tasks}
    lanes = read_layout(section(metrics, "Layout"), titles)
    rows = read_rows(section(metrics, "Dispatches"), lanes)
    written = max((p.stat().st_mtime for p in (metrics_path, tasks_path) if p.exists()), default=0.0)
    changed = max(written, tree_changed(path.parent.parent))
    return Bundle(
        path=path,
        title=title,
        compared=first_line_after(metrics, "Compared against:"),
        elapsed_line=first_line_after(section(metrics, "Totals"), "Elapsed:"),
        tasks=tasks,
        cleanup=cleanup,
        lanes=lanes,
        rows=rows,
        changed=changed,
        written=written,
    )


# --- the coordinator's clock --------------------------------------------------------


def clock_minutes(value: str) -> int | None:
    match = CLOCK.match(value.strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


class FileClock:
    """Minutes since the build's first row, from the HH:MM cells, wrapping once at midnight."""

    def __init__(self, rows: list[Row]):
        starts = [clock_minutes(r.start) for r in rows if clock_minutes(r.start) is not None]
        self.origin = starts[0] if starts else None
        self.latest = 0
        for row in rows:
            for value in (row.start, row.end):
                offset = self.offset(value)
                if offset is not None:
                    self.latest = max(self.latest, offset)

    def offset(self, value: str) -> int | None:
        minutes = clock_minutes(value)
        if minutes is None or self.origin is None:
            return None
        return (minutes - self.origin) % 1440

    def latest_time_of_day(self) -> int | None:
        if self.origin is None:
            return None
        return (self.origin + self.latest) % 1440


def wall_minutes(value: str) -> float | None:
    match = LEADING_DURATION.match(value)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 60 + minutes + seconds / 60


def row_minutes(row: Row, clock: FileClock) -> float | None:
    start, end = clock.offset(row.start), clock.offset(row.end)
    if start is not None and end is not None:
        return float((end - start) % 1440)
    return wall_minutes(row.wall)


def fmt_minutes(minutes: float | None) -> str:
    if minutes is None:
        return ""
    whole = int(round(minutes))
    if whole < 1:
        return "under a minute"
    if whole < 60:
        return f"{whole} min"
    hours, rest = divmod(whole, 60)
    return f"{hours} h {rest:02d} min" if rest else f"{hours} h"


def fmt_ago(seconds: float) -> str:
    if seconds < 45:
        return "just now"
    return f"{fmt_minutes(seconds / 60)} ago"


# --- what the page records itself ---------------------------------------------------


class Observed:
    """When this machine first saw each row open and close."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.started = time.time()
        self.rows: dict[str, dict] = {}
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.started = float(saved.get("started", self.started))
                self.rows = dict(saved.get("rows", {}))
            except (OSError, ValueError):
                pass
        self.first_poll = True

    def update(self, rows: list[Row]) -> None:
        now = time.time()
        changed = False
        for row in rows:
            key = str(row.number)
            seen = self.rows.get(key)
            if seen is None:
                self.rows[key] = {
                    "opened": now,
                    "closed": None if row.open else now,
                    "lower_bound": self.first_poll,
                }
                changed = True
            elif not row.open and seen.get("closed") is None:
                seen["closed"] = now
                changed = True
        self.first_poll = False
        if changed:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps({"started": self.started, "rows": self.rows}, indent=1), encoding="utf-8"
                )
            except OSError:
                pass

    def open_for(self, row: Row) -> tuple[float, bool]:
        """Seconds the row has been open on this clock, and whether that is only a lower bound."""
        seen = self.rows.get(str(row.number))
        if seen is None:
            return 0.0, True
        return time.time() - float(seen["opened"]), bool(seen.get("lower_bound"))


# --- status ---------------------------------------------------------------------------


def outcome_status(outcome: str) -> str:
    head = outcome.strip().lower()
    if any(head.startswith(word) for word in BOUNCE_WORDS):
        return "bounced"
    if any(head.startswith(word) for word in DEAD_WORDS):
        return "dead"
    if any(head.startswith(word) for word in OK_WORDS):
        return "done"
    return "working"


def rows_of(group: Group, rows: list[Row]) -> list[Row]:
    return [row for row in rows if group.number in row.groups]


def group_status(group: Group, rows: list[Row], tasks: dict[int, Task]) -> str:
    mine = rows_of(group, rows)
    if any(row.open for row in mine):
        return "running"
    if group.tasks and all(tasks[t].done for t in group.tasks if t in tasks) and any(t in tasks for t in group.tasks):
        return "done"
    if any("ready to commit" in row.outcome.lower() for row in mine):
        return "done"
    if not mine:
        return "queued"
    latest = max(mine, key=lambda r: r.number)
    status = outcome_status(latest.outcome)
    return "working" if status == "done" else status


def group_minutes(group: Group, rows: list[Row], clock: FileClock) -> float | None:
    """The group's rows summed, so a fix round hours later adds its own minutes and no more."""
    minutes = [m for r in rows_of(group, rows) if (m := row_minutes(r, clock)) is not None]
    return sum(minutes) if minutes else None


def close_out_steps(bundle: Bundle) -> list[tuple[str, str, list[Row]]]:
    rows = bundle.rows
    audit = [r for r in rows if r.kind == "audit"]
    fixes = [r for r in rows if r.kind == "build" and r.lane.lower() == "all"]
    reaper = [r for r in rows if r.kind == "reaper"]
    docs = [r for r in rows if "--docs" in r.what]
    done_box = next((c for c in bundle.cleanup if "/plan done" in c.title), None)

    def status(found: list[Row]) -> str:
        """A closed close-out row counts as done unless its outcome says it bounced or died."""
        if any(r.open for r in found):
            return "running"
        if not found:
            return "queued"
        latest = outcome_status(max(found, key=lambda r: r.number).outcome)
        return latest if latest in ("bounced", "dead") else "done"

    steps = [
        ("audit", status(audit), audit),
        ("fix rounds", status(fixes), fixes),
        ("comment reaper", status(reaper), reaper),
        ("docs review", status(docs), docs),
    ]
    cleanup_done = sum(1 for c in bundle.cleanup if c.done)
    cleanup_status = "queued"
    if bundle.cleanup and cleanup_done == len(bundle.cleanup):
        cleanup_status = "done"
    elif cleanup_done:
        cleanup_status = "working"
    steps.append((f"cleanup {cleanup_done}/{len(bundle.cleanup)}", cleanup_status, []))
    steps.append(("/plan done", "done" if done_box and done_box.done else "queued", []))
    return steps


# --- rendering ----------------------------------------------------------------------


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def inline(value: str) -> str:
    return BACKTICK.sub(lambda m: f"<code>{esc(m.group(1))}</code>", esc(value))


STATUS_WORD = {
    "done": "done",
    "running": "running",
    "working": "in progress",
    "bounced": "bounced",
    "dead": "stopped",
    "queued": "queued",
}


def tag(markup: str, text: str) -> str:
    """`markup` with `{}` filled by already-escaped `text`, or nothing when the text is empty."""
    return markup.format(text) if text else ""


def drift_note(clock: FileClock, changed: float) -> str:
    latest = clock.latest_time_of_day()
    if latest is None or not changed:
        return ""
    local = time.localtime(changed)
    drift = ((latest - (local.tm_hour * 60 + local.tm_min) + 720) % 1440) - 720
    if abs(drift) < CLOCK_DRIFT_MINUTES:
        return ""
    direction = "ahead of" if drift > 0 else "behind"
    return (
        f"The coordinator's clock runs {fmt_minutes(abs(drift))} {direction} this machine's. "
        "Times in the table are its; the running-for counters are this machine's."
    )


def estimate_note(bundle: Bundle, statuses: dict[int, str], clock: FileClock) -> str:
    """The median finished group, times the groups left in the longest lane."""
    groups = [group for lane in bundle.lanes for group in lane.groups]
    finished_minutes = [
        m
        for g in groups
        if statuses[g.number] == "done" and (m := group_minutes(g, bundle.rows, clock)) is not None and m > 0
    ]
    if not finished_minutes:
        return ""
    median = statistics.median(finished_minutes)
    worst_lane, worst_minutes, worst_count = None, 0.0, 0
    for lane in bundle.lanes:
        left = 0.0
        count = 0
        for group in lane.groups:
            status = statuses[group.number]
            if status == "done":
                continue
            count += 1
            spent = group_minutes(group, bundle.rows, clock) or 0.0
            left += median if status == "queued" else max(median - spent, median * 0.25)
        if left > worst_minutes:
            worst_lane, worst_minutes, worst_count = lane, left, count
    if worst_lane is None or not worst_count:
        return ""
    return (
        f"Roughly {fmt_minutes(worst_minutes)} of groups left, if lane {esc(worst_lane.key)}'s "
        f"{worst_count} remaining take the median {fmt_minutes(median)} each. Close-out not counted."
    )


def quiet_class(quiet_minutes: float) -> str:
    if quiet_minutes >= QUIET_RED_MINUTES:
        return "red"
    if quiet_minutes >= QUIET_AMBER_MINUTES:
        return "amber"
    return ""


def head_html(bundle: Bundle, statuses: dict[int, str], steps: list[tuple[str, str, list[Row]]], clock: FileClock,
              finished: bool, now: float) -> str:
    groups = [group for lane in bundle.lanes for group in lane.groups]
    done_groups = sum(1 for s in statuses.values() if s == "done")
    done_tasks = sum(1 for t in bundle.tasks if t.done)
    has_open_row = any(r.open for r in bundle.rows)
    quiet_seconds = now - bundle.changed if bundle.changed else 0.0
    quiet_minutes = quiet_seconds / 60

    elapsed = fmt_minutes(clock.latest) if clock.origin is not None else ""
    if not finished and quiet_minutes >= 1 and elapsed:
        elapsed += f" + {fmt_minutes(quiet_minutes)} quiet"

    segments = [
        f'<i class="seg {statuses[group.number]}" title="{esc(f"G{group.number} {group.label} ({lane.name})")}"></i>'
        for lane in bundle.lanes
        for group in lane.groups
    ] + [f'<i class="seg {status} step" title="{esc(name)}"></i>' for name, status, _ in steps]
    total_units = len(groups) + len(steps)
    done_units = done_groups + sum(1 for _, s, _ in steps if s == "done")
    percent = round(100 * done_units / total_units) if total_units else 0

    updated = fmt_ago(quiet_seconds) if bundle.changed else "never"
    state_word = "finished" if finished else ("running" if has_open_row else "between dispatches")
    estimate = "" if finished else estimate_note(bundle, statuses, clock)
    drift = "" if finished else drift_note(clock, bundle.written)
    return (
        '<section class="head">'
        f'<div class="titles"><span class="eyebrow">build</span><h1>{esc(bundle.title)}</h1></div>'
        '<div class="stats">'
        f'<div class="stat"><b>{percent}%</b><span>{done_groups} of {len(groups)} groups</span></div>'
        f'<div class="stat"><b>{done_tasks}/{len(bundle.tasks)}</b><span>tasks checked</span></div>'
        f'<div class="stat"><b>{esc(elapsed) or "n/a"}</b><span>elapsed</span></div>'
        f'<div class="stat {"" if finished else quiet_class(quiet_minutes)}"><b>{esc(updated)}</b><span>files changed</span></div>'
        f'<div class="stat"><b class="state {"blue" if has_open_row else ""}">{state_word}</b><span>coordinator</span></div>'
        "</div>"
        f'<div class="bar" aria-label="progress">{"".join(segments)}</div>'
        + tag('<p class="eta">{}</p>', estimate)
        + tag('<p class="note">{}</p>', esc(drift))
        + "</section>"
    )


def open_card_html(row: Row, lanes: list[Lane], observed: Observed | None) -> str:
    lane = next((l for l in lanes if l.key == row.lane), None)
    lane_name = lane.name if lane else (row.lane or "whole change")
    seen = ""
    if observed is not None:
        seconds, is_lower_bound = observed.open_for(row)
        seen = f"open for {fmt_minutes(seconds / 60)}"
        if is_lower_bound:
            seen = f"open for at least {fmt_minutes(seconds / 60)}, since before this page started"
    since = f"since {esc(row.start)} on the coordinator's clock" if row.start else ""
    return (
        '<div class="card running">'
        f'<span class="kind">{esc(row.kind or "?")}</span>'
        f'<div class="what">{inline(row.what)}</div>'
        f'<div class="meta"><span>{esc(lane_name)}</span>'
        + tag("<span>{}</span>", seen)
        + tag('<span class="muted">{}</span>', since)
        + tag('<span class="muted">{}</span>', esc(row.new))
        + "</div></div>"
    )


def now_html(bundle: Bundle, observed: Observed | None, finished: bool) -> str:
    open_rows = sorted((r for r in bundle.rows if r.open), key=lambda r: r.number)
    if open_rows:
        cards = "".join(open_card_html(row, bundle.lanes, observed) for row in open_rows)
        return f'<section><h2>Now</h2><div class="cards">{cards}</div></section>'
    if finished:
        return ""
    hint = "Nothing is dispatched. The coordinator is checking, reviewing, or waiting for the owner."
    return f'<section><h2>Now</h2><p class="empty">{hint}</p></section>'


def group_pill_html(group: Group, status: str, rows: list[Row], clock: FileClock) -> str:
    minutes = group_minutes(group, rows, clock)
    count = len(rows_of(group, rows))
    meta = fmt_minutes(minutes) if minutes else ""
    if count:
        meta = f"{meta} · {count} rows" if meta else f"{count} rows"
    return (
        f'<div class="pill {status}" title="{esc(STATUS_WORD[status])}">'
        '<span class="dot"></span>'
        f'<span class="gid">G{group.number}</span>'
        f'<span class="glabel">{esc(group.label.replace("`", ""))}</span>'
        f'<span class="gtasks">{esc(" ".join(f"T{t}" for t in group.tasks))}</span>'
        + tag('<span class="gmeta">{}</span>', esc(meta))
        + "</div>"
    )


def lanes_html(bundle: Bundle, statuses: dict[int, str], steps: list[tuple[str, str, list[Row]]], clock: FileClock) -> str:
    lane_blocks = []
    for lane in bundle.lanes:
        pills = "".join(group_pill_html(g, statuses[g.number], bundle.rows, clock) for g in lane.groups)
        done_here = sum(1 for g in lane.groups if statuses[g.number] == "done")
        lane_blocks.append(
            '<div class="lane">'
            f'<div class="lane-name"><b>{esc(lane.key)}</b><span>{esc(lane.name)}</span>'
            f'<small>{done_here}/{len(lane.groups)}</small></div>'
            f'<div class="pills">{pills}</div></div>'
        )
    step_pills = "".join(
        f'<div class="pill {status}" title="{esc(STATUS_WORD[status])}"><span class="dot"></span>'
        f'<span class="glabel">{esc(name)}</span>'
        + tag('<span class="gmeta">{}</span>', f"{len(found)} rows" if found else "")
        + "</div>"
        for name, status, found in steps
    )
    lane_blocks.append(
        '<div class="lane"><div class="lane-name"><b>·</b><span>close-out</span></div>'
        f'<div class="pills">{step_pills}</div></div>'
    )
    return f'<section><h2>Lanes</h2><div class="lanes">{"".join(lane_blocks)}</div></section>'


def tasks_html(tasks: list[Task]) -> str:
    chips = "".join(
        f'<span class="chip {"done" if t.done else "queued"}" title="{esc(t.title.replace("`", ""))}">T{t.number}</span>'
        for t in tasks
    )
    return f'<section><h2>Tasks</h2><div class="chips">{chips}</div></section>'


def row_html(row: Row, clock: FileClock) -> str:
    status = "running" if row.open else outcome_status(row.outcome)
    if row.kind == "check" and status == "done":
        status = "check"
    span = row.start + (f" to {row.end}" if row.end else "")
    wall = "open" if row.open else fmt_minutes(row_minutes(row, clock))
    return (
        f'<tr class="{status}">'
        f'<td class="num">{row.number}</td>'
        f'<td class="mono">{esc(span)}</td>'
        f'<td class="mono">{esc(wall)}</td>'
        f'<td>{esc(row.lane)}</td>'
        f'<td><span class="kind">{esc(row.kind)}</span></td>'
        f'<td class="what">{inline(row.what)}'
        + tag("<small>{}</small>", esc(row.new) if row.new != "fresh" else "")
        + "</td>"
        f'<td class="mono">{esc(row.tokens)}</td>'
        f'<td class="outcome">{inline(row.outcome)}</td>'
        "</tr>"
    )


def dispatches_html(rows: list[Row], clock: FileClock) -> str:
    """Open rows first, then newest first; the older ones folded."""
    ordered = sorted(rows, key=lambda r: (not r.open, -r.number))
    recent, earlier = ordered[:RECENT_ROWS], ordered[RECENT_ROWS:]
    columns = ("#", "clock", "wall", "lane", "kind", "what", "tokens", "outcome")
    head = "<thead><tr>" + "".join(f"<th>{name}</th>" for name in columns) + "</tr></thead>"

    def table(batch: list[Row]) -> str:
        return f'<table>{head}<tbody>{"".join(row_html(r, clock) for r in batch)}</tbody></table>'

    folded = ""
    if earlier:
        folded = f'<details class="older"><summary>{len(earlier)} earlier rows</summary>{table(earlier)}</details>'
    return f"<section><h2>Dispatches and checks</h2>{table(recent)}{folded}</section>"


def footer_html(bundle: Bundle, now: float) -> str:
    notes = []
    if bundle.elapsed_line:
        notes.append(f"Elapsed: {inline(bundle.elapsed_line)}")
    if bundle.compared:
        notes.append(f"Compared against: {inline(bundle.compared)}")
    notes.append(f"Bundle: <code>{esc(str(bundle.path))}</code>")
    notes.append(f"Rendered {esc(time.strftime('%H:%M:%S', time.localtime(now)))}")
    return f'<footer>{" · ".join(notes)}</footer>'


def render_main(bundle: Bundle, observed: Observed | None, now: float) -> str:
    clock = FileClock(bundle.rows)
    tasks = {task.number: task for task in bundle.tasks}
    groups = [group for lane in bundle.lanes for group in lane.groups]
    statuses = {group.number: group_status(group, bundle.rows, tasks) for group in groups}
    steps = close_out_steps(bundle)
    finished = bool(bundle.elapsed_line) or bool(steps and steps[-1][1] == "done")
    return "".join(
        (
            head_html(bundle, statuses, steps, clock, finished, now),
            now_html(bundle, observed, finished),
            lanes_html(bundle, statuses, steps, clock),
            tasks_html(bundle.tasks),
            dispatches_html(bundle.rows, clock),
            footer_html(bundle, now),
        )
    )


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #f4f2ee; --card: #fbfaf7; --ink: #1f1d1a; --muted: #716c64; --line: #e4e0d8; --code: #ecebe7;
  --green: #2d8a58; --blue: #2f6fd0; --amber: #c7870f; --red: #cc3a3a; --grey: #a6a199;
  --green-soft: #e1f1e7; --blue-soft: #e2ebfa; --amber-soft: #f9eed3; --red-soft: #f9dede; --grey-soft: #edebe6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #141618; --card: #1b1e22; --ink: #e8e4dc; --muted: #9b958c; --line: #2a2e34; --code: #262a30;
    --green: #5fc78f; --blue: #7ea9f0; --amber: #e5ae48; --red: #ee7070; --grey: #6c6862;
    --green-soft: #1b3326; --blue-soft: #1c2940; --amber-soft: #382d14; --red-soft: #3b1e1e; --grey-soft: #23262a;
  }
}
* { box-sizing: border-box; }
html { background: var(--bg); color: var(--ink); }
body {
  margin: 0; padding: 28px 24px 48px;
  font: 15px/1.45 ui-sans-serif, -apple-system, "Segoe UI", Inter, Roboto, sans-serif;
  font-feature-settings: "tnum" 1;
}
main { max-width: 1180px; margin: 0 auto; }
code { font: 0.92em ui-monospace, "SF Mono", Menlo, Consolas, monospace; background: var(--code); padding: 0 4px; border-radius: 4px; }
.mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 0.9em; white-space: nowrap; }
h1 { font-size: 26px; font-weight: 650; letter-spacing: -0.01em; margin: 0; }
h2 { font-size: 12px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); margin: 36px 0 12px; }
.eyebrow { font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
.head { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 22px 24px 20px; }
.stats { display: flex; flex-wrap: wrap; gap: 8px 28px; margin: 18px 0 16px; }
.stat b { display: block; font-size: 20px; font-weight: 650; }
.stat span { font-size: 12px; color: var(--muted); }
.stat.amber b { color: var(--amber); }
.stat.red b { color: var(--red); }
.state.blue { color: var(--blue); }
.bar { display: flex; gap: 3px; height: 14px; }
.seg { flex: 1; border-radius: 3px; background: var(--grey-soft); min-width: 6px; }
.seg.step { flex: 0.6; }
.seg.done { background: var(--green); }
.seg.running { background: var(--blue); animation: pulse 1.6s ease-in-out infinite; }
.seg.working { background: var(--blue-soft); box-shadow: inset 0 0 0 1.5px var(--blue); }
.seg.bounced { background: var(--amber); }
.seg.dead { background: var(--red); }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
.eta { margin: 14px 0 0; color: var(--muted); font-size: 13px; }
.note { margin: 8px 0 0; font-size: 13px; color: var(--amber); }
.empty { color: var(--muted); margin: 0; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
.card { background: var(--card); border: 1px solid var(--line); border-left: 4px solid var(--blue); border-radius: 12px; padding: 14px 16px; }
.card .what { font-weight: 600; margin: 6px 0 8px; }
.card .meta { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 13px; }
.muted { color: var(--muted); }
.kind { display: inline-block; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); background: var(--grey-soft); padding: 1px 7px; border-radius: 999px; }
.lanes { display: flex; flex-direction: column; gap: 10px; }
.lane { display: grid; grid-template-columns: 150px 1fr; gap: 12px; align-items: start; }
.lane-name { padding-top: 8px; }
.lane-name b { display: inline-block; width: 22px; }
.lane-name span { font-size: 13px; }
.lane-name small { display: block; color: var(--muted); font-size: 12px; margin-left: 22px; }
.pills { display: flex; flex-wrap: wrap; gap: 8px; }
.pill { display: inline-flex; align-items: center; gap: 8px; padding: 7px 12px 7px 10px; border-radius: 999px; border: 1px solid var(--line); background: var(--card); font-size: 13px; }
.pill .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--grey); flex: none; }
.pill .gid { font-weight: 650; }
.pill .gtasks, .pill .gmeta { color: var(--muted); font-size: 12px; }
.pill.done { background: var(--green-soft); border-color: transparent; }
.pill.done .dot { background: var(--green); }
.pill.running { background: var(--blue-soft); border-color: var(--blue); }
.pill.running .dot { background: var(--blue); animation: pulse 1.6s ease-in-out infinite; }
.pill.working { border-color: var(--blue); }
.pill.working .dot { background: var(--blue); }
.pill.bounced { background: var(--amber-soft); border-color: transparent; }
.pill.bounced .dot { background: var(--amber); }
.pill.dead { background: var(--red-soft); border-color: transparent; }
.pill.dead .dot { background: var(--red); }
.pill.queued { color: var(--muted); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font-size: 12px; font-weight: 600; padding: 3px 9px; border-radius: 6px; background: var(--grey-soft); color: var(--muted); }
.chip.done { background: var(--green-soft); color: var(--green); }
table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; font-size: 13px; }
th { text-align: left; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); font-weight: 600; padding: 10px 10px 6px; border-bottom: 1px solid var(--line); }
td { padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
tr td:first-child { border-left: 3px solid var(--grey); }
tr.done td:first-child { border-left-color: var(--green); }
tr.check td:first-child { border-left-color: var(--green-soft); }
tr.running td:first-child { border-left-color: var(--blue); }
tr.bounced td:first-child { border-left-color: var(--amber); }
tr.dead td:first-child { border-left-color: var(--red); }
tr.working td:first-child { border-left-color: var(--grey); }
td.num { color: var(--muted); width: 3ch; }
td.what { min-width: 220px; }
td.what small { display: block; color: var(--muted); }
td.outcome { color: var(--muted); min-width: 260px; }
tr.running td.outcome::before { content: "running"; color: var(--blue); font-weight: 600; }
details.older { margin-top: 12px; }
details.older summary { cursor: pointer; color: var(--muted); font-size: 13px; margin-bottom: 10px; }
footer { margin-top: 32px; font-size: 12px; color: var(--muted); }
@media (max-width: 720px) {
  body { padding: 16px 12px 40px; }
  .lane { grid-template-columns: 1fr; gap: 4px; }
  .stats { gap: 8px 18px; }
  td.outcome, td.what { min-width: 0; }
}
</style>
</head>
<body>
<main>__MAIN__</main>
__SCRIPT__
</body>
</html>
"""

SCRIPT = """<script>
(function () {
  var main = document.querySelector("main");
  var url = location.pathname + "?fragment=1";
  async function tick() {
    try {
      var response = await fetch(url, { cache: "no-store" });
      if (!response.ok) return;
      var opened = Array.from(main.querySelectorAll("details")).map(function (d) { return d.open; });
      main.innerHTML = await response.text();
      main.querySelectorAll("details").forEach(function (d, i) { d.open = opened[i] || false; });
      var marker = main.querySelector("[data-title]");
      if (marker) document.title = marker.dataset.title;
    } catch (error) {}
  }
  setInterval(tick, 5000);
})();
</script>"""


def render_page(bundle: Bundle, observed: Observed | None, live: bool) -> str:
    body = render_main(bundle, observed, time.time())
    return (
        PAGE.replace("__TITLE__", esc(bundle.title))
        .replace("__MAIN__", body)
        .replace("__SCRIPT__", SCRIPT if live else "")
    )


# --- serving --------------------------------------------------------------------------


def default_port(path: pathlib.Path) -> int:
    return 41000 + zlib.crc32(str(path).encode("utf-8")) % 1000


def state_path(bundle: pathlib.Path) -> pathlib.Path:
    number = bundle.name.split("-", 1)[0]
    return bundle.parent.parent / "_tmp" / f"{number}-build" / "dashboard-state.json"


class Watcher:
    def __init__(self, bundle: pathlib.Path):
        self.bundle_path = bundle
        self.observed = Observed(state_path(bundle))
        self.lock = threading.Lock()
        self.bundle = read_bundle(bundle)
        self.observed.update(self.bundle.rows)

    def poll(self) -> None:
        while True:
            time.sleep(POLL_SECONDS)
            try:
                fresh = read_bundle(self.bundle_path)
            except OSError:
                continue
            self.observed.update(fresh.rows)
            with self.lock:
                self.bundle = fresh

    def page(self, fragment: bool) -> str:
        with self.lock:
            bundle = self.bundle
        if fragment:
            body = render_main(bundle, self.observed, time.time())
            return f'<div data-title="{esc(bundle.title)}"></div>{body}'
        return render_page(bundle, self.observed, live=True)


def serve(bundle: pathlib.Path, port: int) -> int:
    watcher = Watcher(bundle)
    url = f"http://127.0.0.1:{port}/"

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            path, _, query = self.path.partition("?")
            if path == "/health":
                self.reply(str(bundle), "text/plain; charset=utf-8")
            elif path == "/":
                self.reply(watcher.page(fragment="fragment=1" in query), "text/html; charset=utf-8")
            else:
                self.send_error(404)

        def reply(self, text: str, content_type: str) -> None:
            data = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as error:
        try:
            with urllib.request.urlopen(url + "health", timeout=2) as answer:
                served = answer.read().decode("utf-8", errors="replace").strip()
        except (urllib.error.URLError, OSError):
            served = ""
        if served == str(bundle):
            print(f"Dashboard already serving this bundle at {url}")
            return 0
        print(f"Port {port} is taken ({error}); pass --port with a free one.", file=sys.stderr)
        return 1

    threading.Thread(target=watcher.poll, daemon=True).start()
    print(f"Dashboard for {bundle.name} at {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", help="the bundle directory, such as _todo/037-search-on-the-component")
    parser.add_argument("--port", type=int, help="override the port derived from the bundle's path")
    parser.add_argument("--once", metavar="FILE", help="write the page to FILE and exit")
    args = parser.parse_args()

    bundle = pathlib.Path(args.bundle).resolve()
    if not bundle.is_dir():
        print(f"not a directory: {bundle}", file=sys.stderr)
        return 2

    if args.once:
        page = render_page(read_bundle(bundle), None, live=False)
        pathlib.Path(args.once).write_text(page, encoding="utf-8")
        print(f"Wrote {args.once}")
        return 0

    return serve(bundle, args.port or default_port(bundle))


if __name__ == "__main__":
    sys.exit(main())
