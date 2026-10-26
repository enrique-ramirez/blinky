---
name: build
description: Build an approved spec bundle from `_todo/` by coordinating agents rather than writing the code. Groups the tasks, runs independent groups in parallel lanes, gets a failing test before each builder, verifies what comes back, and reviews each group's design as it lands. Use when someone says to build, implement or ship a spec.
argument-hint: <n>|<slug> [task <n>] [--no-dashboard]
---

# Build

**You coordinate. You do not build.** No product code, no test, not the thirty-second fix. Your work is reading, dispatching, checking what comes back, and telling the owner what is true. `doctrine/agents.md` binds you as it binds the agents.

## The gate

Read `spec.md`. Refuse, naming the line, unless:

- `Status: approved`
- Open questions is empty and no `[?]` survives
- every acceptance criterion has a row in `plan.md`'s verification table
- a file scope is named, or a sentence says why the change is tree-wide

An unapproved spec belongs to `/plan`. Uncommitted work already in the tree makes every diff check useless: say so and stop.

## Measuring

Copy `templates/todo/build-metrics.md` into the bundle before the first dispatch. Time the profile's commands once, unless the profile gives their cost, and record them as the baseline: what a check costs decides where it runs, and a filter that skips running but not compiling saves nothing.

Open a row as each dispatch goes out, with its start from `date +%H:%M` and the rest blank, and complete it as the dispatch closes: end, wall clock, the tokens and tool uses the harness reports, fresh or resumed, a one-line outcome whose first word is `accepted`, `bounced`, `stopped` or `died`. A time from memory drifts by hours over a build. A resumed agent reports its whole context, so record the difference from its previous total. Your own checks get rows without tokens, written as they close. Compare against the *Totals* and *Levers* of the last measured bundle in `_done/`. Fill in *Where it went* and *Levers* once the build closes.

Serve the progress page before the first dispatch, unless the owner passed `--no-dashboard`: run `build-dashboard.py` with the bundle's path in the background, from `.claude/bin/` in a copy install or `${CLAUDE_PLUGIN_ROOT}/bin/` in a plugin one. It prints a URL; put it in the message that says the layout back. If it fails to start, say so and carry on. The page reads the metrics file and `tasks.md`, shows an open row as running, and counts quiet from the newest write anywhere in the working tree, so a builder editing files far from the bundle keeps it moving.

## Groups and lanes

**A group** is the tasks whose tests span the same files: one test-writer and one builder between them. A task whose tests span two toolchains or three surfaces is too big; split it and tell the owner.

**A lane** is groups that must run in order: those sharing a file, a build output, a binary, a port or a generated artifact. Lanes sharing none run at once. Two lanes in one toolchain usually share something: the lockfile, a generated file, a module both import, one type check or one build that goes red for either. Name what is shared; two groups of one app that share none of these run at once, each running only the checks over its own files. Where one lane blocks another only through a shared artifact (a contract, a schema, generated types), have it written first from the spec.

Write the layout into the metrics file and say it back to the owner in a few lines.

## The brief

A builder handed its seam spends its tool calls building; one that has to find it spends them searching. Every brief carries:

- the task lines and criteria, quoted
- the file scope, and the files in other lanes that are not this agent's
- the seam verbatim: signature, shape, name, the line it sits beside
- the neighbour to copy, as `file:line`
- what stays red after this round and whose it is
- the exact commands
- anything an earlier agent found that this one would walk into

Tell it to read only the spec sections the brief names, and that saying where the brief is wrong is part of the job. Where agents in different lanes need the same name, refusal string or shape, dictate it to both.

## The loop

For each group, in its lane:

1. **Say the group back** in one line: tasks, criteria, scope, targeted commands.
2. **Dispatch `test-writer`** for any criterion without a test.
3. **Run the test yourself.** It must fail on the missing behaviour, not on an import error, and not pass. Read the break-check table: a break nothing caught is a missing case. Either failure goes back to `test-writer`.
4. **Record the test files' checksums** with `shasum` into `_tmp/`. A new test file is untracked, so only a checksum shows whether the builder touched it.
5. **Dispatch `task-builder`** with the test paths marked read-only.
6. **Check what came back**, cheapest first:

| check | how |
|---|---|
| test files untouched | `shasum -c`. Not negotiable: a builder that edited its test has invalidated the task |
| nothing outside the scope moved | `git diff --name-only` and `git status --porcelain` against the scope |
| nothing stashed | `git stash list` unchanged |
| the test passes | `test-writer`'s command |
| the targeted check passes | the command from `plan.md`, for this group only |
| each task's done-test holds | read it yourself |
| no markdown file created | `git diff --name-only --diff-filter=A -- '*.md'` and the untracked list are empty. The documentation map is the owner's |
| the diff does only the task | read it. Extra work bounces, however good |

   Then read the report against the briefs sent to other lanes. A name, shape or string chosen differently from what another agent was told is cheap to fix now and costly at the audit.

7. **Accept or bounce.**
8. **`style-reviewer` over the accepted group's diff**, skipped for a pure rename, relocation or generated file. If it edits code, re-run the targeted check.

## Keeping agents warm

Resume an agent rather than spawning a fresh one, for a bounce, a follow-up, or the next group in its lane: a fresh agent pays for reading the profile, the agent files and the bundle again. Hand over to a fresh agent after about five resumes, with a few lines: what is built, what is red and whose, the seams.

An agent silent well past its siblings may have died, and an interruption from the owner can end one. Check, and re-dispatch what died.

## Bouncing

Never fix it yourself. Name the defect, quote the check, send it back resumed, and say what to keep. **Twice failed is a spec problem.** Do not dispatch a third time: say whether the task is two tasks, the criterion is ambiguous, the plan's pattern does not fit, or the scope is wrong, and take it to `/plan resume`.

A reported flake gets one check from you first: run the case alone several times.

## Questions go to the owner

A builder that stopped on an unstated case did its job. Bring the question up with the readings it could not choose between; the answer becomes a criterion or a *Not doing* line through `/plan resume`, then the task runs again. An engineering default that changes nothing a user sees (reusing an existing limit, following the neighbour's error shape) is yours; say you answered it.

## Commits are the owner's

After each accepted group, check its boxes in `tasks.md`, report, and wait. Where the owner says to carry on without committing, keep a list of the commit boundaries and say each time how many groups are stacked up uncommitted.

## The audit

Once every box is checked, including the cleanup task, dispatch `change-auditor` over the whole change, naming the risks from `plan.md`, the seams between lanes, and anything an agent flagged and left. Do not collapse its four axes into a verdict. Route what it finds:

| finding | route |
|---|---|
| red check inside one group's files | resume that builder with the output |
| red check across groups | a seam: append a task to `tasks.md` and dispatch it |
| green that should not be | `test-writer`; the task that did it is not done |
| criterion missing or partial | uncheck the box and re-dispatch |
| criterion met by logic that does not hold | `test-writer` first, then the builder |
| unasked-for behaviour | back to the builder to remove. If the owner wants it, `/plan resume` |
| a seam defect | append a task and dispatch it |
| a defect inside one task | resume that builder |
| noted | relay it; it does not become a round |
| out of scope | relay it to `/plan` |

Fixes in different lanes go out together. After a fix round, re-run only the axes it could touch. The same blocking finding surviving two rounds is a spec problem: take it to `/plan resume`.

## Closing out

1. The audit, until nothing blocks.
2. `style-reviewer` over the audit's fixes, if there were any.
3. `comment-reaper` over the whole change, once, unless `git diff HEAD` adds no comment line. Tell it a comment-only edit runs nothing.
4. A documentation pass over what the change deleted or renamed: grep every documentation file in the repository, the ones the diff never touched included, for each name and each mechanism, and hand `style-reviewer` the files that still describe it.
5. The Green axis again, if anything changed code after the audit.
6. `/plan done <n>`.

A backgrounded long check keeps its whole output in a file.

## Reporting

Per group, four lines: the tasks, what changed, what verified it, what is ready to commit. At the end: the metrics totals against the bundle compared with, which criteria are proved by which test, which were reviewed rather than tested, what builders noticed and left, and the single next thing. Do not narrate the dispatches.
