# Rules every agent in this kit follows

**The profile.** Read `.claude/review-profile.md` first and treat it as fact: the boundary, excluded paths, functional directives, commands, what cannot be verified here, the voice. If it is missing, say so in your report and name the repository facts you had to guess. Never touch an excluded path.

**Git.** Never run a git command that writes: no `commit`, `add`, `checkout`, `restore`, `switch`, `stash`, `reset`, `rm`. Reading (`diff`, `status`, `log`, `show`) is fine. To see the tree before your change, save your diff under `_tmp/`, `git apply -R` it, look, then `git apply` it back. To undo your own edit, rewrite the file.

**The default target** is uncommitted work: `git diff HEAD` plus every untracked file in `git status --porcelain --untracked-files=all`, read whole. `git diff` alone misses staged work.

**Product decisions are the owner's.** Anything that adds, removes or changes what the software does for a user (a feature, a refusal, a default, an information architecture choice, a security default) you stop and ask about, or report as a finding if you meet it in a diff. The outcome is not written into any file.

**The suite.** Run only the targeted commands the profile names, for what you touched. Never the full suite. Check that a run executed what you meant: a selector that matches nothing often reports success.

**The tree is shared.** Other agents may be writing in it. An error in a file you did not touch is not yours: run again before reporting it, and never fix it.

**Markdown.** Never create a new markdown file outside `_todo/`, `_done/` and `_tmp/`. `doctrine/documentation.md` decides whether a thing should be a name, a test or a sentence, and which file the sentence goes in. Read it before writing prose into the tree.

**Writing.** Before writing documentation, read `voice/constructions.md` and `voice/register.txt`; the hook checks the mechanical half on every comment and test title you write. Reviewers, the planner and the auditor also apply them to their reports, in the persona the profile's *Voice* section names (`voice/personas/<name>.md`, or `voice/personas/house.md` where that file is absent). Rewrite a flagged sentence; never swap the word.

**Reports** are lists, not prose. Each finding has `file:line`, what is wrong, and the fix. Say what you ran and what it reported, and what you could not check.
