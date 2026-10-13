---
name: review-profile
description: Fill in or refresh this repository's review profile by reading the code, so the review agents know its boundary, its exclusions and its commands. Use when setting up the review kit in a project, or when the profile has gone stale.
---

# Review profile

`.claude/review-profile.md` is what every kit agent treats as fact about this repository. A placeholder left in it makes them fall back to generic behaviour.

If the kit is not installed yet, run `<kit>/bin/install.sh <this-repo> --mode plugin`, or `--mode copy` for a self-contained repository. The script never runs git.

## Fill it in from the code

| section | where to look |
|---|---|
| Boundary | linter import rules, top-level directories, any architecture document. No rule worth naming: say so |
| Excluded paths | `.gitignore`, generated-file headers, vendored directories, lockfiles |
| Functional directives | suppression comments the build depends on |
| Doc comments | whether a linter or convention requires a docstring |
| Documentation map | the markdown files that exist, and what each holds today |
| Where an external fact belongs | the document already holding facts about outside systems, for a fact governing more than one file |
| Commands | `package.json` scripts, the Makefile, CI. Targeted forms, with their cost |
| What cannot be verified here | platform-specific sources, hardware, live services; for a page, what jsdom cannot see |
| User interface | the test runner's config and setup file, the render helper the tests share, the lint config's import rules, the tokens or theme file, the locale folder, the screen or design file the look follows |

## Ask the owner

- **Voice:** which persona governs prose here. `house` unless somebody's name is on the output.
- **Product decisions:** who decides, and what an agent does on finding one in a diff.

## Local files

- `.claude/style-patterns.local.txt`: hostnames, addresses and machine names that must not reach a commit, and `-regex` allow lines for a line that has to quote a banned phrase. Gitignored. Ask for the entries; never harvest them from shell history or the environment.
- `.claude/register.local.txt`: local register phrases, switches for built-in checks, and the `=claude-budget` and `=claude-root-budget` word budgets.
- `.claude/style-exempt.txt`: globs the hook skips. Prefer an allow line to exempting a file.

## Check it

Write a scratch file with an em dash and a phrase from the register list, confirm the hook reports both, and delete it. Report what you filled in and the two questions left for the owner.
