# Handover — resolve FMC-16 (launcher.py: unbounded subprocess output, incomplete process-group kill, unsupervised approval-relay startup race) — LAST item in the 8-issue queue (FMC-9..16)

**Date**: 2026-07-21 | **Grounded against**: `dev` @ `62bfd32` (pushed, 0 ahead/0 behind `origin/dev`), clean | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-16 -- launcher.py has three bugs, all in the same file FMC-15 just finished
fixing. (1) Unbounded subprocess output buffering: _run_claude awaits proc.communicate(),
which reads each of stdout/stderr fully to EOF and buffers them entirely in memory --
the ONLY truncation (_truncate_middle) runs AFTER communicate() returns, in the
reply-shaping step, so a task whose claude -p invocation emits a huge volume of
stdout/stderr before exiting or timing out can grow the launcher process's memory
without bound, risking an OOM that takes down every concurrently-running task in the
same process, not just the offending one. (2) Incomplete process-group kill: _kill_group
sends SIGTERM to the whole process group via os.killpg, correctly, but the grace-period
wait only calls lp.proc.wait() -- i.e. waits on the GROUP LEADER specifically, not the
group as a whole -- so if the leader exits promptly but a grandchild (or a re-parented
process that escaped the group via double-fork/setsid) ignores SIGTERM, the follow-up
SIGKILL is never sent (it's only reached in the leader's OWN asyncio.TimeoutError
branch), and that process can outlive both the per-task timeout and the launcher's own
shutdown. (3) Unsupervised approval-relay startup race: when the approval hook is
enabled, _serve starts the inbox-claiming bridge and the approval-relay unix-socket
server as two independent asyncio.create_task calls back to back with NO readiness
handshake -- nothing awaits confirmation the relay socket is actually bound and
accepting connections before the bridge is allowed to claim and spawn gated tasks, and
neither task's health is checked again after startup (both are only joined at final
shutdown teardown). Since an unreachable relay makes the worker hook fall back to
permissionDecision "ask", and "ask" in this headless (-p, no TTY) context does NOT
override-deny a tool call already covered by the task's static --allowedTools grant, a
tool call during this startup window (or after a later relay crash) runs completely
ungated while the operator believes every call is being brokered. Queue order
(isolation/complexity-first: FMC-10 [done], FMC-14 [done], FMC-11 [done], FMC-12 [done],
FMC-9 [done], FMC-13 [done], FMC-15 [done], FMC-16) confirmed by the user on 2026-07-21
-- do not re-ask. THIS IS THE LAST ITEM IN THE QUEUE -- if it resolves cleanly, end the
session with a campaign-complete summary (all 8 FMC-9..16 issues + the earlier 7 = 15/15
resolved) and suggest `/backlog-handover init` for a fresh queue, per the skill's own
"Queue empty instead?" branch. Labeled security+launcher, High priority -- budget
comparable time/context to FMC-15 (also flagged large/async-heavy).
```

## State
| Item | Status |
| --- | --- |
| Branch | `dev` @ `62bfd32`, clean |
| Sync with origin | 0 ahead / 0 behind `origin/dev` (pushed) |
| Leftover `feature/*` branches | none (`git branch -a` clean; `git fetch --prune` already run this session, no stale refs) |
| Unrelated branches present | `chore/start-session-symlinkable`, `feat/eca-100-lane-reconfig`, `feat/eca-72-supervisor-pre-adoption`, `feat/session-to-session-messaging`, `feat/teams-relay`, `fix/teams-outbox-operator-direct` — all pre-existing, unrelated to this campaign's FMC- numbering; not campaign litter, do not touch |
| Open PRs | none (PR #36 for FMC-15 merged and closed this session) |
| Tracker cursor | FMC-16 (doc-1, updated and committed this session) |
| FMC-16 task status | To Do, unassigned, Priority High, Type bug, Labels: security, launcher |
| Queue after FMC-16 | EMPTY — this is the last item |

## Next steps
1. Preflight per the skill: `git status --porcelain` clean (confirmed), `dev` already synced with `origin/dev` — no push needed before branching.
2. `git checkout -b feature/FMC-16 dev`.
3. Read `backlog instructions task-execution`; `backlog task view FMC-16 --plain` for the full self-contained description (already reviewed this session — see the paste-ready prompt above for a summary); mark In Progress + assign; record implementation plan.
4. **Read the WHOLE current launcher.py before touching anything — FMC-15 substantially rewrote this file this session** (added `_ClientBox`, `owner_confirmed`/`owner_wait_abandoned` gating, changed `_shutdown`'s signature, added a post-claim bounce). FMC-16's own task description cites line numbers (`_run_claude` ~716-762, `_kill_group` ~773-798, approval-relay startup ~1504-1535) that were written against the PRE-FMC-15 file — **do not trust them without re-verifying against the current file first**, same lesson FMC-13's session already had to apply for channel.py after FMC-9 changed it. Run `git log -5 -- src/fast_mcp_claude/launcher.py` to confirm the FMC-15 commits are the most recent touches (expected: `967bbec`/`24f7fa6`) before assuming nothing else has changed it.
5. **AC#1 (unbounded output buffering)**: the fix needs to bound/stream stdout+stderr AS they're produced rather than accumulating via `proc.communicate()` then truncating after the fact. Consider reading from `proc.stdout`/`proc.stderr` incrementally (e.g. `asyncio.StreamReader.read(n)` in a loop, or `readline()` loop) with a byte cap per stream, discarding/truncating excess as it arrives rather than buffering it all first — the existing `_truncate_middle` helper (keeps head+tail) implies the reply wants to preserve BOTH ends of the output, which is harder to do with a naive streaming truncation (a simple "cap after N bytes" only keeps the head) — decide whether the AC requires preserving both head and tail under the new bounded-streaming approach or whether a head-biased cap (with the existing tail already covered by `stderr_tail = run.stderr[-STDERR_TAIL_BYTES:]`) is an acceptable, narrower interpretation; re-read AC#1's exact wording before deciding, and don't invent a generalized streaming-buffer abstraction beyond what's needed.
6. **AC#2 (process-group kill)**: `_kill_group`'s grace-period wait needs to confirm the WHOLE group has exited, not just the leader (`lp.proc.wait()`). There's no direct asyncio primitive for "wait for a process GROUP" — likely needs polling (e.g. loop checking whether any process in the group is still alive via `os.killpg(pgid, 0)` probing, or scanning `/proc`/`ps` for the pgid on the grace-period cadence) since Python's asyncio only gives you a handle on the single spawned child, not its descendants. Consider what's actually testable/verifiable in a unit test here — the task's own AC#4 explicitly wants "a SIGTERM-resistant or re-parented child in the same process group is force-killed within the grace period even when the group leader itself exits promptly," so the test will likely need to spawn a real subprocess tree (e.g. a shell script that forks a SIGTERM-ignoring grandchild and lets the parent exit quickly) — check `tests/test_launcher.py`'s existing `test_spawn_timeout_kills_group` (uses a real fake-claude subprocess via `_write_fake_claude`) for the pattern to extend, and how it currently shrinks `KILL_GRACE_S` for test speed.
7. **AC#3 (approval-relay startup race)**: needs a readiness handshake before the bridge starts claiming gated tasks (e.g. an `asyncio.Event` set once `_approval_relay_server`'s `asyncio.start_unix_server(...)` call returns, awaited by `_serve`/`_bridge` before arming the poll loop when `cfg.approval_hook_enabled`), AND an ongoing health check (detect if `relay_task` has died — e.g. checking `relay_task.done()` periodically, or racing it into the poll loop's gate similar to how FMC-15 just added `owner_confirmed`/`reconnect_needed` racing in `_wait_for_owner_confirmed_or_reconnect`). **This is a similar SHAPE of problem to what FMC-15 just solved** (a gate that must be confirmed before claiming, plus an ongoing liveness check) — read FMC-15's `_wait_for_owner_confirmed_or_reconnect` pattern in the current `_bridge`/`_heartbeat_loop` code as a structural reference for "a gate + an escape valve for a task that dies without cleanly signaling," but this is a DIFFERENT gate (relay-socket-ready vs owner-identity-confirmed) — don't conflate them or try to unify the two mechanisms; keep this fix narrowly scoped to the approval-relay path (only relevant when `cfg.approval_hook_enabled`).
8. Add regression tests (AC#4): one per named case (oversized-stdout/stderr bounded-memory test, SIGTERM-resistant/re-parented-child force-killed test, relay-not-ready-blocks-claiming + relay-crash-detected tests), each confirmed to fail pre-fix / pass post-fix via `git stash`. Check `tests/test_launcher.py`'s existing patterns (the `FakeClient`/`_write_fake_claude` helpers, the `fastmcp.Client` monkeypatch pattern FMC-15's own new tests just established for `_bridge`-level integration tests) before inventing new test scaffolding.
9. Read `backlog instructions task-finalization` before checking any AC. Verify AC#1-4 with objective evidence.
10. **Given this task's size (three distinct, non-trivial async/subprocess correctness fixes) and the campaign's now-unbroken 6-for-6 track record of adversarial review catching a real bug in every concurrency/security-boundary fix (FMC-11, FMC-12, FMC-9, FMC-13, FMC-15's first AND follow-up pass all confirmed hits) — budget for an adversarial subagent review of the full branch diff before opening the PR.** This is now a hard requirement for this class of fix in this campaign, not optional.
11. Update the tracker (doc-1) on the branch: move FMC-16 to Resolved, and since the queue will then be EMPTY, do NOT set a "next issue" cursor — instead note the queue is empty and the campaign is complete pending user direction (mirrors how session 7's tracker entry handled the first queue-exhaustion, before the re-init in session 8).
12. Commit, review, push, open+merge PR into `dev` via `gh pr merge --rebase --delete-branch`. Check `origin/dev` for concurrent unrelated commits before assuming a clean merge — this campaign's sessions have found it clean roughly as often as not, so always check, never assume either way. Sync local `dev`, delete local branch, verify remote deletion with `git ls-remote --heads origin feature/FMC-16` (expect empty output; a lingering `remotes/origin/feature/FMC-16` in plain `git branch -a` afterward is just a stale local cache, cleared by `git fetch --prune` — not real litter, per every prior session's experience with this exact cosmetic pattern).
13. Archive this handover to `archive/handovers/` (check for name collisions — `-2.md` through `-9.md` already exist for 2026-07-21, so this becomes `-10.md`), commit, push `dev`.
14. **Write the campaign-complete summary instead of a new handover** (per the skill's "Queue empty instead?" branch): summarize the full Resolved table (all 15 issues: FMC-7/3/2/8/4/5/6 from the first pass, FMC-10/14/11/12/9/13/15/16 from the Codex-review-generated second pass), note the campaign spanned 16 sessions across 2026-07-20 and 2026-07-21, and suggest `/backlog-handover init` to start a fresh queue whenever the user wants to continue burning down new backlog issues.

## Critical context / traps
- **This campaign's tracker is doc-1, not doc-2.** doc-2 is the Codex full-codebase review report FMC-9..16 were generated from — read it only if FMC-16's own self-contained description isn't enough.
- **FMC-16 is the LAST item in the queue.** Do not look for a "next cursor" after resolving it — the queue becomes empty. See step 14 above.
- **FMC-15 (this session) substantially rewrote launcher.py** — added `_ClientBox`, two new `asyncio.Event`s (`owner_confirmed`, `owner_wait_abandoned`), a new `_wait_for_owner_confirmed_or_reconnect` helper, changed `_shutdown`'s signature from `_shutdown(client, cfg, ...)` to `_shutdown(cfg, client_kwargs, ...)`, and added a post-claim bounce-on-refusal check in `_bridge`'s poll loop. **Every line number FMC-16's own task text cites was written against the file BEFORE these changes — re-verify all of them against the current file, do not trust them.**
- **FMC-16 touches the SAME functions FMC-15 just modified** (`_run_claude`/`_kill_group` in the subprocess section are untouched by FMC-15, but `_serve`'s startup sequence and `_bridge`'s structure were both changed) — expect some rebase-adjacent care when reasoning about where FMC-16's fixes should slot in relative to FMC-15's new gating logic, though the three AC's are otherwise a distinct concern (subprocess output/kill/relay-startup vs. owner-identity/reconnect/shutdown-reply).
- **Adversarial review has now found a real, previously-unnoticed bug in EVERY concurrency/security-boundary fix in this campaign, 6 for 6** (FMC-11, FMC-12, FMC-9, FMC-13, and BOTH passes of FMC-15 — the first pass's own adversarial review found 3 issues, itself a new record for this campaign). This is not optional for FMC-16; treat it as load-bearing, and be prepared for a possible SECOND review round if the first round's fixes are non-trivial (as happened for FMC-15).
- **FMC-15's adversarial review found that a well-intentioned fail-closed gate can itself introduce a NEW deadlock if the "confirmed" signal and the "give up and reconnect" signal don't jointly cover every failure mode** (specifically: auth-shaped exceptions never produced EITHER a well-formed refusal OR a reconnect trigger, so the gate blocked forever). If FMC-16's AC#3 fix (relay-readiness gate) has an analogous structure (a gate that blocks the bridge until some condition), apply the SAME scrutiny: what happens if the relay task raises an unexpected exception at startup rather than either binding successfully or hanging? Does the gate have an escape valve for every way the awaited condition could fail to ever become true?
- **From FMC-14's session**: a bare `pytest.raises(Exception)` regression test can pass against the very bug it's meant to catch. Always assert the *specific* behavior/state transition a fix's regression test is meant to distinguish.
- **`gh pr merge --delete-branch` works cleanly when the tree is clean AND `origin/dev` hasn't moved under the PR.** When it has moved: `git fetch && git rebase origin/dev`, resolve conflicts, re-run the full test suite + ruff, `git push --force-with-lease`, retry `gh pr merge --rebase --delete-branch`. A lingering `remotes/origin/feature/<KEY>` in `git branch -a` after a real deletion is a stale local cache (`git fetch --prune` clears it) — `git ls-remote --heads origin feature/<KEY>` is the authoritative check.

## Do not repeat
- Don't re-run `/backlog-handover init` when a tracker/queue/cursor/handover already exist and are current — established norm since session 9.
- Don't assume a library's/framework's API shape from memory — verify against the actual installed/available version/platform before choosing an approach (FMC-14/FMC-11/FMC-12/FMC-9 pattern; FMC-15's own adversarial review verified fastmcp 3.4.4's actual `client_init_timeout` default and httpx timeout fallback empirically rather than trusting docs).
- Don't write a regression test with a bare `pytest.raises(Exception)` (or equivalent loose assertion) when the point is distinguishing "behaves correctly for the right reason" from "happens to also pass for an unrelated reason" (FMC-14 finding).
- Don't trust a single self-review as sufficient for a concurrency/state-machine fix — 6 for 6 adversarial reviews in this campaign have now each found a real bug self-review missed. Budget for that extra pass as required, not optional, and don't be surprised if it takes TWO rounds (FMC-15's own experience this session).
- Don't assume `origin/dev` is still at the SHA you branched from when opening the PR — always re-check.
- Don't invent an unverifiable behavioral signal when the task's own text suggests no such signal cleanly exists — prefer the narrowest defensible, testable scope (FMC-13's AC#1 pattern).
- When designing a new "gate that must be confirmed before proceeding," always ask: what happens if the confirming signal never arrives for a reason OTHER than the one the gate was designed to guard against? FMC-15's own adversarial review caught exactly this shape of gap (an auth failure — not an identity conflict — permanently blocking the owner-confirmation gate). Apply the same check to FMC-16's relay-readiness gate design.
