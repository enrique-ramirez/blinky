---
name: review
description: Run the style-reviewer then comment-reaper pass over a target, or with `--docs` audit whole documentation files against the doctrine and the word budget. Use before asking for a commit, when someone says to review a diff, a commit or a path, or to audit the docs. Takes an optional target; defaults to uncommitted changes.
argument-hint: [<path>|<ref>|<range>] | --docs [<path>]
---

# Review

`style-reviewer` settles structure and naming, then `comment-reaper` decides which comments survive what is left; the other order writes comments for code about to move. `humanize` can follow. Neither agent hunts defects or runs the suite; that is `change-auditor`.

## The target

| argument | target |
|---|---|
| nothing | uncommitted work |
| a ref or range | that commit or range |
| a path | the files under it |
| `--docs [<path>]` | a documentation audit of every `CLAUDE.md`, `AGENTS.md`, `README.md`, `CONTRIBUTING.md` and `ARCHITECTURE.md` under the path, the repository root by default |

Say the target back before starting. For a path, count the files: above about forty for the reviewer or thirty for the reaper, propose batches that follow the directory structure and let the caller choose.

## A diff or a path

Launch `style-reviewer` with the target and any worry the caller mentioned, then `comment-reaper` with the same target. Relay what each changed and flagged; from the reaper, its counts, the comments it kept with the questions their readers failed, every fact it moved with its word count, and its code findings. A comment that disagrees with its code means one of them is wrong.

## `--docs`

List the files with `python3 <kit>/hooks/check-prose.py --words <file>...`, which prints each one's prose words and budget as the hook counts them (`<kit>` is `.claude` in a copy install). Launch `style-reviewer` in documentation-audit mode over the list, one agent per batch of about fifteen files grouped by directory, each handed the full list so it can grep for duplication outside its batch. The reaper does not run. Relay, per file: words against its budget, the findings, and the proposed cuts with the words each saves. Cuts beyond the unambiguous ones are the owner's to accept.

## Afterwards

Neither agent commits. If either changed code rather than only comments or docs, run the profile's targeted checks and report them. Say what is ready and stop.
