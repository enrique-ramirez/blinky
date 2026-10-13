# Tasks NNN slug

Each task names its files, the acceptance criteria it serves, and the observable that says it is done. Tasks whose tests live in the same files sit together, because `/build` gives them one test-writer and one builder. A builder does the tasks it was given, and stops.

- [ ] T1 <what changes> | `path/to/file` | criteria: 1, 2 | done when: <observable>
- [ ] T2 

## Cleanup

Last, and never skipped. The change is not finished while the scaffolding that built it is still in the tree.

- [ ] Delete what this spec added and no longer uses: flags behind a shipped path, fixtures, commented-out code, scratch scripts under `_tmp/`.
- [ ] Check the acceptance criteria against what was built. Where one drifted, say which side is wrong.
- [ ] Run `/review` over the whole change.
- [ ] `/plan done <n>`: move this bundle to `_done/NNN-slug/` and drop its row from `_todo/INDEX.md`.
