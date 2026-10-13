---
name: plan
description: Turn an idea into a spec bundle under `_todo/` that another agent can build from, then approve it and close it out. Use when someone describes work to be done rather than asking for it to be done, or says to spec, plan or scope something.
argument-hint: [<brief>] | resume <n> | approve <n> | done <n>
---

# Plan

`spec-planner` does the judgement. This picks the job and does the bookkeeping.

| argument | job |
|---|---|
| a brief, or nothing | a new bundle. With nothing, ask for one sentence about the problem and stop |
| `resume <n>` or a slug | carry on an open bundle's interrogation |
| `approve <n>` | record the owner's approval |
| `done <n>` | close it out |

Say the job and the bundle back before starting.

## New

Check `_todo/INDEX.md` first: a brief matching an open row is a `resume`. Launch `spec-planner` with the brief verbatim and any constraint the caller mentioned. Relay its assumptions and questions without answering them for the owner or softening the ones that are product decisions. Feed the answers back to the same agent. Where a third round still moves the acceptance criteria, say the idea is unsettled and offer to split it.

## Resume

Read the bundle and lead with where it stands: open questions left, criteria without a test in `plan.md`. Then launch `spec-planner` on it. A bundle untouched for weeks is usually dead; ask before spending a round.

## Approve

Refuse, and say why, when:

- Open questions is not empty, or a `[?]` survives in `spec.md`
- a criterion has no row in the plan's verification table
- Not doing is empty
- there is no file scope and no sentence saying why the change is tree-wide

Otherwise set `Status: approved` and write the Approved line as a fact: what was agreed, against which draft, the date. Never the words the owner typed.

## Done

1. Walk the cleanup task: dead flags, unloaded fixtures, probes in `_tmp/`, commented-out code.
2. Check each criterion against what shipped. Where one drifted, say which side is wrong rather than editing the spec to match.
3. Run `/review` over the change if it has not run.
4. Run `/review --docs` over every documentation file the change touched, and relay what it finds.
5. `mv` the bundle to `_done/NNN-slug/` and drop its row from the index.

Nothing is archived into the repository. A fact about an outside system the work turned up goes to the owner as one proposed present-tense line for the file the profile's map names.
