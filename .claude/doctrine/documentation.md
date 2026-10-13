# Where knowledge lives

**The code and its tests are the documentation.** Markdown holds what those two cannot express, and describes the code as it is now.

## Before writing a sentence of prose

Ask whether the code or a test can say it instead. Usually one can:

| about to write | write instead |
|---|---|
| what this function does | a name that says it |
| why this value | a named constant |
| how this is meant to behave | a test whose title is that sentence |
| what happens in the empty case | a test for the empty case |
| what this directory contains | nothing. A reader can list it |
| "do not change X, because Y" | a test that fails when X changes |

A test title is a sentence: `rejects a second registration for the same credential id` is documentation that runs.

## A guard is not documentation

A paragraph that exists to stop somebody undoing a decision is not documentation. If a change would break something, a test fails. If no test can catch it, the owner is the guard, not a file. A "do not" sentence in a doc names the test that holds it, or it goes.

What stays is a fact about a system outside the repository: what a browser, a library, an operating system or a third-party service does. State it in the present tense, as a fact about that system, naming it.

## Who reads what

| file | reader | holds |
|---|---|---|
| code and tests | everyone | what is built, and what it promises |
| `README.md` | whoever uses that folder's thing | what it is and how to use it |
| `CONTRIBUTING.md` | developers, human and agent | how to write code here |
| `ARCHITECTURE.md` | developers | how the parts fit |
| `CLAUDE.md`, `AGENTS.md` | agents only | pointers, and traps an agent cannot read off the code |

A README's reader changes with its folder: beside an app it is the app's user, in a shared package it is the developer importing it. A component folder has no README: its props type and its test titles say what it does. A UI package's README shows the developer importing it how a component is rendered and tested.

`CLAUDE.md` is the smallest of these. Craft that applies to anyone belongs in `CONTRIBUTING.md`. The root file points once at where style, usage and architecture live, and no file lists the others: Claude Code loads a nested `CLAUDE.md` by itself when an agent reads in that folder. The hook holds a word budget on each.

The repository's profile names which of these files exist and what each holds. Where it disagrees with this file, the profile wins.

## Rules

1. **The code as it is now.** No history, changelog, decision record, thought process, status or roadmap. Git holds what changed; the owner holds what is next.
2. **One fact, one home.** A second copy drifts, and nobody is told which is wrong.
3. **Route by reader, not by topic.**
4. **Never create a new markdown file unasked.** A new file changes this map, and the map is the owner's.
5. **Nothing committed names `_todo`, `_done` or `_tmp`.** They are gitignored.
6. **Third person.** No *I*, *we*, *you* in documentation. Where the reader has to act, the imperative carries it: *fill in the profile*.
7. **Short.** Every surviving sentence was chosen over a name or a test.

Rule 6 governs documentation. Agent, skill and voice files are instructions, and address their reader directly.

## External facts in comments

An external fact that constrains one line is a comment beside that line. It moves into a document only when it applies to more than one file.
