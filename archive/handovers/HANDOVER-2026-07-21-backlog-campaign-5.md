# Handover — resolve FMC-11 (files.py/validation.py TOCTOU symlink race) — third issue of the 8-issue queue (FMC-9..16)

**Date**: 2026-07-21 | **Grounded against**: `dev` @ `29b3262` (about to be pushed), clean, 1 commit ahead of `origin/dev` | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-11 -- validate_workspace_path (utils/validation.py ~165-186) checks
containment once and returns a resolved Path; list_files/read_file/write_file
(tools/files.py) each perform a LATER, separate filesystem operation on that
same path string with no fd-based re-check and no no-follow protection --
a classic TOCTOU: a co-located process can swap a path component for a
symlink pointing outside WORKSPACE_ROOTS in the window between the check and
the actual scandir/read/write, defeating the sandbox. Queue order (isolation/
complexity-first: FMC-10 [done], FMC-14 [done], FMC-11, FMC-12, FMC-9, FMC-13,
FMC-15, FMC-16) confirmed by the user on 2026-07-21 -- do not re-ask before
taking the next item.
```

## State
| Item | Status |
| --- | --- |
| Branch | `dev` @ `29b3262`, clean |
| Sync with origin | 1 ahead / 0 behind `origin/dev` (this session's archive-handover commit not yet pushed -- push it as part of restore's preflight, or it happens automatically if you push before starting FMC-11's branch) |
| Leftover `feature/*` branches | none (checked local + remote, pruned) |
| Open PRs | none (`gh pr list --state open` empty) |
| Tracker cursor | FMC-11 (doc-1, updated and committed this session) |
| FMC-11 task status | To Do, unassigned, Priority High, Type bug |
| Queue after FMC-11 | FMC-12, FMC-9, FMC-13, FMC-15, FMC-16 (5 more after this one) |

## Next steps
1. Preflight per the skill: confirm `git status --porcelain` is clean and push the 1-ahead commit to `origin/dev` if not already done.
2. `git checkout -b feature/FMC-11 dev`.
3. Read `backlog instructions task-execution`; `backlog task view FMC-11 --plain` for the full self-contained description (already reviewed this session -- see Critical context below for a summary); mark In Progress + assign; record implementation plan.
4. Read `src/fast_mcp_claude/utils/validation.py`'s `validate_workspace_path` (~lines 165-186) and all three call sites in `src/fast_mcp_claude/tools/files.py`: `list_files` (~line 30, `os.scandir(resolved)` at ~line 41), `read_file` (~line 75, `resolved.read_text()`/`read_bytes()` at ~lines 92/95), `write_file` (~line 117, `resolved.parent.mkdir(...)` at ~line 148 and `resolved.write_bytes(...)` at ~line 149).
5. Choose a fix approach and verify it against Python's actual `os`/`pathlib` capabilities before committing to it (don't assume API shape from memory, same discipline FMC-14 required for fastmcp). The task's own fix-direction note suggests either: (a) open path components via a directory file descriptor with no-follow/beneath-only semantics (e.g. `os.open` with `O_NOFOLLOW`, or Python 3.12's `pathlib`/`os` support for `dir_fd` + `O_NOFOLLOW`, resolving relative to an opened root fd rather than re-traversing a path string), or (b) an equivalent descriptor-based re-verification immediately before each actual I/O call (open with no-follow, then confirm the fd's real path via `/proc/self/fd/<n>` equivalent or `os.path.realpath` on the opened descriptor is still inside `WORKSPACE_ROOTS` before doing further I/O through that descriptor, not through the path string again). Must cover all three call sites, including `write_file`'s parent-directory `mkdir` step (a second traversal that can itself be raced).
6. Add regression tests (AC#4) that simulate the race: substitute a symlink for a path component after validation but before the tool's real filesystem operation runs (e.g. monkeypatch/mock the point between check and use, or structure the test to swap the symlink via a hook/callback if the implementation allows injecting one, or reason about the narrower window achievable in a single-threaded test and assert the fix's guard triggers even when a symlink is already in place at the swapped position). Cover list_files, read_file, and write_file separately per AC#1-3.
7. Read `backlog instructions task-finalization` before checking any AC. Verify AC#1-3 with objective evidence (each operation fails closed, not just "looks right").
8. Update the tracker (doc-1) on the branch: move FMC-11 to Resolved, advance cursor to FMC-12, append the session-11 log entry.
9. Commit, review (self or adversarial subagent -- FMC-14's adversarial pass caught a real false-positive-test bug a self-review would likely have missed, so lean toward using one again, especially for a security-boundary fix like this), push, open+merge PR into `dev` via `gh pr merge --rebase --delete-branch`, sync local `dev`, delete local branch.
10. **Known gh quirk from FMC-14's session**: if `gh pr merge` fails with a git-level error (e.g. "local changes would be overwritten by checkout") because the working tree wasn't clean (a `backlog task edit --append-notes`/similar run after the last commit left a file dirty), the PR may have ALREADY been merged on GitHub's side even though `--delete-branch` never ran locally. After any `gh pr merge` failure, re-run `git status`, commit anything dirty, retry `gh pr merge` (it will report "already merged" if so), then explicitly check `git ls-remote --heads origin feature/<KEY>` -- if the branch still exists remotely despite a successful merge, delete it manually (`git push origin --delete feature/<KEY>`). Don't just trust `--delete-branch` silently worked.
11. Archive this handover to `archive/handovers/` (check for name collisions -- `-2.md` through `-4.md` already exist from earlier today, so this becomes `-5.md`), commit, write the session-11 handover for cursor FMC-12, push `dev`.

## Critical context / traps
- **This campaign's tracker is doc-1, not doc-2.** doc-2 is the Codex full-codebase review report FMC-9..16 were generated from -- read it only if FMC-11's own self-contained description isn't enough.
- **FMC-11 is explicitly distinct from FMC-4 (already Done).** FMC-4 fixed a check-*order* bug (existence check ran before containment check, leaking info via exception type) and confirmed symlink-resolve logic is sound for symlinks that already exist AT CHECK TIME. FMC-11 is a check-to-use *race window* bug (TOCTOU) -- a symlink swapped in AFTER the check but BEFORE the actual filesystem operation. The task's own description explicitly warns not to treat FMC-4 as having closed this gap.
- **FMC-12 (next in queue) touches `services/store.py`**, which FMC-5 already modified -- read FMC-5's resolved-table entry in doc-1 before touching Notifier/cleanup code again when you get there.
- **FMC-9 and FMC-13 (later in the queue) are more design-ambiguous than a typical bugfix**: they require inventing an actual trust/provenance mechanism for the channel sidecar's admin/operator authority. Expect a documented scoping judgment call when you reach them, same as FMC-4/FMC-8/FMC-10/FMC-14 already made.
- **FMC-9/FMC-13 share `channel.py`, and FMC-15/FMC-16 share `launcher.py`** -- the queue deliberately sequences each pair back-to-back to reduce rebase churn.
- Two related-but-out-of-scope notes from session 8 (doc-1's session log, not queued): a MEDIUM auth.py finding is FMC-3's already-accepted intentional tradeoff, and a MEDIUM store.py finding looks like a subtle regression in FMC-5's own retry-loop rewrite. Neither is part of FMC-9..16's scope.
- **From FMC-14's session (session 10)**: an adversarial subagent review caught a real bug in the new test file itself -- a bare `pytest.raises(Exception)` regression test that would also pass against the very bug it was meant to catch (since the pre-fix `TypeError` is also an `Exception`). Always assert the *specific* exception type/attributes a fix's regression test is meant to distinguish, not a bare `Exception`. Worth deliberately checking any new race-simulation test in FMC-11 for the same false-positive-test risk (e.g. "operation raised *some* error" isn't proof it failed closed for the *right* reason -- assert the specific denial, like a `PermissionDeniedError`/`ValidationError`/OS-level `EEXIST`/no-follow error, not just any exception).
- **gh pr merge / --delete-branch can silently not delete the remote branch** if the command errors on a dirty local working tree mid-operation (see Next step 10) -- verify with `git ls-remote --heads origin feature/<KEY>` after every merge, don't just trust the flag.

## Do not repeat
- Don't re-run `/backlog-handover init` when a tracker/queue/cursor/handover already exist and are current -- this is the established norm for this campaign (confirmed explicitly in session 9).
- Don't assume a library's API shape (fastmcp, or here `os`/`pathlib`'s no-follow/dir_fd support) from memory or general docs -- verify against the actual installed/available version before choosing an approach. This was the specific root cause FMC-14 fixed, and the same discipline applies to FMC-11's O_NOFOLLOW/dir_fd approach.
- Don't write a regression test with a bare `pytest.raises(Exception)` (or equivalent loose assertion) when the whole point is distinguishing "fails for the right reason" from "fails for any reason, including the original bug." FMC-14's adversarial review caught exactly this.
- Don't trust `gh pr merge --delete-branch` succeeded without checking `git ls-remote --heads origin feature/<KEY>` afterward -- FMC-14's session found it can silently skip the delete step on a dirty-tree error even though the PR merge itself went through.
