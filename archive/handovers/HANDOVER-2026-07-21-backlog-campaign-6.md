# Handover — resolve FMC-12 (store.py hub-drain races + unbounded Notifier growth) — fourth issue of the 8-issue queue (FMC-9..16)

**Date**: 2026-07-21 | **Grounded against**: `dev` @ `c171a98` (about to be pushed), clean, 1 commit ahead of `origin/dev` | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-12 -- services/store.py has 2 bugs. (1) list_pending_teams_sends/
list_pending_session_ops are plain SELECTs with no claim step (unlike
pop_next_for_worker's atomic select+update pattern), so two concurrent hub
drainers can both observe the same pending row and BOTH perform the real-world
side effect (post to Teams twice, execute a session-relay op twice) -- the
complete_*'s status=pending guard only protects the completion WRITE, not the
double side-effect, since the claim never happened atomically with the read.
(2) Notifier._get (unconditionally creates an asyncio.Event for any key,
inbox:<recipient_session>/pubsub:<channel> included) never gets forgotten for
inbox:/pubsub: keys -- FMC-5's forget() only evicts keys tied to a deleted DB
row, and FMC-5 explicitly assumed inbox:/pubsub: keys are safe left unforgotten
because they're "bounded by live identities" -- an assumption this finding
shows is false, since recipient_session/channel are validated only for FORMAT
(SESSION_RE/CHANNEL_RE), never for actually corresponding to a live identity,
so any authenticated caller can grow the Notifier's memory unboundedly with
fabricated identity/channel values + timeout=0 calls. Queue order (isolation/
complexity-first: FMC-10 [done], FMC-14 [done], FMC-11 [done], FMC-12, FMC-9,
FMC-13, FMC-15, FMC-16) confirmed by the user on 2026-07-21 -- do not re-ask
before taking the next item.
```

## State
| Item | Status |
| --- | --- |
| Branch | `dev` @ `c171a98`, clean |
| Sync with origin | 1 ahead / 0 behind `origin/dev` (this session's archive-handover commit not yet pushed -- push it as part of restore's preflight, or it happens automatically if you push before starting FMC-12's branch) |
| Leftover `feature/*` branches (this campaign) | none (checked local + remote, pruned) |
| Unrelated active worktree | `feature/ECA-101` exists as a SEPARATE, currently-checked-out worktree at `/private/tmp/claude-501/-Users-jdnewhouse-repos-evolv-coder-agent/.../scratchpad/fmc-eca101` -- this is another concurrent session's in-progress work on an unrelated task (ECA-101 in evolv-coder-agent numbering, not this campaign's FMC- numbering). NOT campaign litter -- do not touch it, it will keep showing up in `git branch -a` for as long as that other session's worktree exists. |
| Open PRs | none (`gh pr list --state open` empty) |
| Tracker cursor | FMC-12 (doc-1, updated and committed this session) |
| FMC-12 task status | To Do, unassigned, Priority High, Type bug |
| Queue after FMC-12 | FMC-9, FMC-13, FMC-15, FMC-16 (4 more after this one) |

## Next steps
1. Preflight per the skill: confirm `git status --porcelain` is clean and push the 1-ahead commit to `origin/dev` if not already done. `feature/ECA-101` will appear in `git branch -a` -- that's the unrelated worktree noted above, not a crashed session of this campaign; ignore it.
2. `git checkout -b feature/FMC-12 dev`.
3. Read `backlog instructions task-execution`; `backlog task view FMC-12 --plain` for the full self-contained description (already reviewed this session -- see Critical context below for a summary); mark In Progress + assign; record implementation plan.
4. Read `src/fast_mcp_claude/services/store.py`: `pop_next_for_worker` (~line 307 onward, the ALREADY-correct atomic select+update pattern to mirror), `list_pending_teams_sends` (~lines 572-579), `complete_teams_send` (~lines 593-608), `list_pending_session_ops` (~lines 663-670), `complete_session_op` (~lines 684-706), `Notifier._get` (~lines 143-148), and FMC-5's `forget()` (added by the already-Done FMC-5 task -- read that task's Resolved-table entry in doc-1 first, since FMC-12 is explicitly a gap in FMC-5's own fix, not a duplicate).
5. **Sub-bug 1 (AC#1/#2, non-atomic drain claims)**: make `list_pending_teams_sends`/`list_pending_session_ops` (or their `wait_for_pending_*` callers) atomically transition the row's status away from `pending` at read time, under the same `_db_lock`, mirroring `pop_next_for_worker`'s select+update-in-one-critical-section pattern. Watch for: `complete_teams_send`/`complete_session_op`'s own `status=pending` write-guard will need to change to match whatever the new "claimed" status is (e.g. `status=claimed` instead of `status=pending` in the completion UPDATE's WHERE clause) -- read both completion functions carefully so the claim-then-complete lifecycle stays consistent end to end, and don't break the existing passing tests in `tests/test_teams_outbox.py`/`tests/test_session_relay.py` that already exercise the current (racy) claim path.
6. **Sub-bug 2 (AC#3, unbounded Notifier growth)**: `Notifier._get`/`forget()` in `services/store.py` need inbox:/pubsub: keys to become boundable too -- this is a genuinely more open-ended fix than sub-bug 1 (the task doesn't prescribe a mechanism). Consider: bounding via an LRU/max-size cap on `_events` with eviction of the least-recently-used non-actively-waited key (the existing waiter-refcount guard from FMC-5 already prevents evicting a key someone is actively parked on), or a TTL-based sweep for inbox:/pubsub: keys with zero waiters that haven't been touched in N minutes, or tying eviction to something else bounded. Whichever approach, it must not break the legitimate case: a real, live session's `inbox:<session>` key or a real pubsub `channel:<channel>` key must still work correctly for long-poll waits that are actually in flight or recently used.
7. Add regression tests (AC#4) that fail against the pre-fix code and pass against the fix: for sub-bug 1, simulate two concurrent `list_pending_teams_sends`/`list_pending_session_ops` calls racing for the same row and assert only one can claim it (and/or assert the row's status after both calls proves only one side effect should be performed) -- assert the *specific* claimed-vs-pending state transition, not a bare exception, per the FMC-14 lesson (see Do not repeat below). For sub-bug 2, drive `Notifier._get` (or `wait_for_instruction`/`subscribe` with fabricated identities/channels and `timeout=0`) many times with fresh fabricated keys and assert `len(Notifier._events)` (or equivalent) stays bounded rather than growing linearly with the number of calls.
8. Read `backlog instructions task-finalization` before checking any AC. Verify AC#1-4 with objective evidence.
9. Update the tracker (doc-1) on the branch: move FMC-12 to Resolved, advance cursor to FMC-9, append the session-12 log entry.
10. Commit, review (self or adversarial subagent -- lean toward using one again for a concurrency/security-boundary fix like this, same as FMC-11's session did; it found a real short-write regression there), push, open+merge PR into `dev` via `gh pr merge --rebase --delete-branch`, sync local `dev`, delete local branch.
11. Verify the remote branch actually got deleted: `git ls-remote --heads origin feature/FMC-12` should be empty after the merge. FMC-11's session found `gh pr merge --delete-branch` DOES correctly delete both local and remote branches (and even auto-switches you off the feature branch) when the working tree is clean at merge time -- the "silent skip" quirk from FMC-14's session was specifically caused by a dirty tree during the merge, so keep the tree clean through step 10 and this should just work. Still worth the one-line verification since it's cheap.
12. Archive this handover to `archive/handovers/` (check for name collisions -- `-2.md` through `-5.md` already exist from earlier today, so this becomes `-6.md`), commit, write the session-12 handover for cursor FMC-9, push `dev`.

## Critical context / traps
- **This campaign's tracker is doc-1, not doc-2.** doc-2 is the Codex full-codebase review report FMC-9..16 were generated from -- read it only if FMC-12's own self-contained description isn't enough.
- **FMC-12 is explicitly a gap in FMC-5's already-completed fix, not a duplicate.** FMC-5 (session 6) fixed 4 Notifier/long-poll bugs including unbounded growth for message/approval/teams_outbox/session_relay-keyed entries (all tied to real, deletable DB rows) via `forget()`. FMC-5's own implementation notes explicitly assumed `inbox:`/`pubsub:` keys were safe to leave unforgotten because they're "bounded by live identities" -- FMC-12 shows that assumption is false once you account for adversarially-fabricated (format-valid but not-actually-live) identity/channel values. Read FMC-5's Resolved-table entry in doc-1 before touching `Notifier`/`forget()`/`_cleanup_once` again.
- **FMC-9 and FMC-13 (next in the queue after FMC-12) are more design-ambiguous than a typical bugfix**: they require inventing an actual trust/provenance mechanism for the channel sidecar's admin/operator authority. Expect a documented scoping judgment call when you reach them, same as FMC-4/FMC-8/FMC-10/FMC-14/FMC-11 already made.
- **FMC-9/FMC-13 share `channel.py`, and FMC-15/FMC-16 share `launcher.py`** -- the queue deliberately sequences each pair back-to-back to reduce rebase churn.
- Two related-but-out-of-scope notes from session 8 (doc-1's session log, not queued): a MEDIUM auth.py finding is FMC-3's already-accepted intentional tradeoff, and a MEDIUM store.py finding looks like a subtle regression in FMC-5's own retry-loop rewrite. Neither is part of FMC-9..16's scope -- but note the second one is ALSO in store.py, the same file FMC-12 touches; don't conflate it with FMC-12's two sub-bugs when you're in that file.
- **From FMC-14's session (session 10)**: an adversarial subagent review caught a real bug in a new test file -- a bare `pytest.raises(Exception)` regression test that would also pass against the very bug it was meant to catch. Always assert the *specific* exception type/attribute/state transition a fix's regression test is meant to distinguish, not a bare `Exception`.
- **From FMC-11's session (session 11)**: an adversarial subagent review of a security-boundary fix caught a real (non-security) regression -- a rewritten `os.write()` call that discarded its return value, silently mis-reporting bytes written on a short write. Worth deliberately running an adversarial review again for FMC-12's concurrency fix, since race-condition fixes are exactly the kind of change where a self-review tends to miss interleaving edge cases.
- **`gh pr merge --delete-branch` works cleanly when the tree is clean at merge time** (confirmed in FMC-11's session: both local and remote branches were correctly removed, and it auto-switched off the feature branch) -- the "silently skips the delete" quirk documented from FMC-14's session was specifically triggered by a DIRTY working tree during the merge. Keep the tree clean through the merge step and this should not recur; still verify with `git ls-remote --heads origin feature/<KEY>` since it's a one-line check.
- **`feature/ECA-101` is not part of this campaign** -- it's a separate worktree from a concurrent, unrelated session (see State table above). It will keep appearing in `git branch -a`/`git worktree list` for as long as that other session is active; do not delete it or treat it as a crashed-session artifact of this campaign.

## Do not repeat
- Don't re-run `/backlog-handover init` when a tracker/queue/cursor/handover already exist and are current -- this is the established norm for this campaign (confirmed explicitly in session 9).
- Don't assume a library's API shape from memory or general docs -- verify against the actual installed/available version/platform before choosing an approach. FMC-14 (fastmcp Client API) and FMC-11 (`os.scandir` does NOT support `dir_fd` on this macOS install, confirmed via a standalone empirical probe before writing any fix code) both hit this; the same discipline applies to whatever `Notifier`/SQLite locking primitives you reach for in FMC-12's sub-bug 1 concurrency fix -- verify `_db_lock`'s actual scope/behavior by reading it, don't assume.
- Don't write a regression test with a bare `pytest.raises(Exception)` (or equivalent loose assertion) when the whole point is distinguishing "fails/behaves correctly for the right reason" from "happens to also pass for an unrelated reason." FMC-14's adversarial review caught exactly this; FMC-11's tests deliberately paired each error-code assertion with a direct filesystem-state assertion for the same reason.
- Don't trust a single-threaded "simulate the race by mutating state between step A and step B" test as proof of thread-safety for sub-bug 1 without also thinking through whether SQLite's actual locking (and this project's `_db_lock` asyncio lock) can even let two truly concurrent callers interleave the way the bug describes -- read `_db_lock`'s usage across `store.py` before designing the test, since an overly-narrow simulated race might not reflect what's actually possible under the real lock discipline (or might reveal the fix needs to hold the lock across a wider critical section than the read+update pair alone).
