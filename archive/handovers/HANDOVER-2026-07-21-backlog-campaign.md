# Handover — resolve FMC-5, Notifier/long-poll correctness bugs in services/store.py

**Date**: 2026-07-21 | **Grounded against**: `dev` @ `ac5d6713b9e8e11f849262d31b48853f2b9e6f3e`, clean working tree, in sync with `origin/dev` (pushed this session) | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-5 — Fix Notifier/long-poll correctness bugs in services/store.py (Medium
priority, reliability/store; 4 sub-bugs, 5 acceptance criteria — see below). Queue order
confirmed by user on 2026-07-20 (docs first, then High->Medium->Low severity: FMC-7(done)
-> FMC-3(done) -> FMC-2(done) -> FMC-8(done) -> FMC-4(done) -> FMC-5 -> FMC-6); do not
re-ask.

Session 5 resolved FMC-4 (sandbox existence oracle + missing body-size caps) and merged it
into dev via PR #25 (rebase-merge, commits f598b95 + 568cd2b). Fix 1 reordered
validate_workspace_path so WORKSPACE_ROOTS containment is checked before must_exist,
closing a probe that let an authenticated peer test for arbitrary file existence outside
the sandbox via distinguishable error types — confirmed the pre-fix bug empirically by
exec'ing the OLD function standalone against a tmp dir (existing out-of-sandbox path ->
PermissionDeniedError, missing -> ValidationError; two different exception types). Fix 2
added a generic validate_json_object_size() helper + 3 new cap constants, wired into all
6 flagged structured-JSON fields across messaging.py/permissions.py/presence.py/
session_relay.py/teams_outbox.py. Added 22 tests (277 total, up from 257). Independent
adversarial-review subagent found no blocking issues before the PR was opened.

FMC-5 is explicitly flagged in the tracker as THE RISKIEST remaining change in the queue
— it touches core long-poll infrastructure (Notifier, the shared wait_for pattern every
blocking tool depends on) rather than an isolated module. Read the task fresh
(`backlog task view FMC-5 --plain`) and re-verify every line number before editing — this
handover's numbers are current as of dev @ ac5d671 but may have drifted.
```

## State

| Item | Status |
| --- | --- |
| Tracker doc | doc-1, cursor advanced to FMC-5, FMC-4 moved to Resolved with evidence |
| FMC-4 | Done — merged to `dev` via PR #25 (rebase-merge, commits `f598b95`+`568cd2b`) |
| Cursor issue | FMC-5 (queue position 1 of 1 remaining after this — FMC-6 is the only other item), status: To Do |
| Queue order | FMC-5 → FMC-6 |
| Branch | `dev` (this repo's campaign default branch — not `main`) |
| Working tree | Clean as of `ac5d671` |
| Remote sync | `dev` and `origin/dev` both at `ac5d671` (pushed this session) |
| `feature/*` branches | None (local or remote — `feature/FMC-4` was deleted both sides automatically by `gh pr merge --delete-branch`, since the merge ran while that branch was checked out; confirmed via `git ls-remote --heads origin feature/FMC-4` returning nothing) |
| Open PRs | None (`gh pr list --state open` empty) |
| `.claude/handovers/` | This file is the only active one; the FMC-4 handover was archived to `archive/handovers/HANDOVER-2026-07-20-backlog-campaign-5.md` (name collision with existing unsuffixed/`-2`/`-3`/`-4` entries at the same date+topic — suffixed `-5`), committed (`ac5d671`) |

## Next steps

1. Run the per-issue lifecycle on FMC-5: `git checkout -b feature/FMC-5 dev`, read `backlog instructions task-execution`, mark FMC-5 In Progress + assign `@claude`, record an implementation plan.
2. FMC-5 has 4 independent sub-bugs plus a required test (read the task fresh — `backlog task view FMC-5 --plain` — line numbers below are as of `dev @ ac5d671`, verify current before editing):
   - **Sub-bug 1 (broadcast never wakes identity-scoped waiters)**: `services/store.py:252-255` — `enqueue_message` only calls `self._notifier.notify(self._inbox_key(recipient_session))` plus, when `recipient_session is not None`, ALSO notifies `inbox:*` (the broadcast key) so a broadcast-listening worker wakes for an addressed message. But the reverse case is missing: when `recipient_session is None` (a genuine broadcast), only `inbox:*` gets notified — no identity-scoped `inbox:<foo>` waiter is woken, even though `pop_next_for_worker` (store.py:258-301) DOES return NULL-recipient rows to an identity-scoped caller. A worker parked on `inbox:foo` therefore sleeps the full `poll_max_wait_s` before ever seeing a broadcast. Fix requires notifying every currently-waiting identity-scoped key on a broadcast enqueue — but `Notifier` has no registry of "who is currently waiting on which key" beyond the `_events` dict itself (which holds an event per key ever waited on, live or not). Think about how to reach "all live identity waiters" without over-notifying keys nobody is listening on (harmless but wasteful) vs. under-notifying (the bug). AC #5 requires a new test for this path specifically (existing `test_storage.py:131`-area coverage only covers `pop_next_for_worker`, not the notify/wake path).
   - **Sub-bug 2 (Notifier._events unbounded growth)**: `services/store.py:135-136` (the `_events: dict[str, asyncio.Event]` field) — one permanent event per key ever waited on (`outbox:{message_id}`, `approval:{approval_id}`, `teams_outbox:{id}`, `session_relay:{id}`, ...), never pruned, in a process meant to run under pm2 indefinitely. Needs some eviction strategy — likely tied into the existing `_cleanup_once` sweep (store.py:885+) since that's the only periodic hook that already knows which message/approval/teams/session-relay IDs are resolved and safe to forget. Watch for a race: don't evict a key's event while a waiter might still be parked on it.
   - **Sub-bug 3 (`wait_for` re-checks exactly once)**: `services/store.py:151-177` (the `Notifier.wait_for` method) — on wakeup it calls `check()` exactly once; if that returns `None` (lost a race to another waiter), `wait_for` returns `None` immediately instead of continuing to wait out the caller's remaining timeout. This needs a loop that re-waits on the (possibly-replaced, since `notify()` swaps in a fresh `Event`) event for whatever time budget remains, re-checking each time it wakes, until the original `timeout` is exhausted. Get the remaining-time bookkeeping right (e.g. via `time.monotonic()`), and re-fetch the event reference each iteration since `notify()` at store.py:145-149 replaces `_events[key]` with a new `Event` after firing.
   - **Sub-bug 4 (STATUS_EXPIRED unobservable / dead code)**: `services/store.py:885-901` (`_cleanup_once`) — the UPDATE marks stale `queued`/`delivered` rows as `expired` using `cutoff`, then the VERY NEXT statement DELETEs rows in `(replied, cancelled, expired)` using the SAME `cutoff`, so every row the UPDATE just touched gets immediately deleted in the same sweep — a waiter never observes `status: "expired"`, it just gets a `NotFoundError` (`messaging.py:112`-ish), and the `STATUS_EXPIRED` branch at `store.py:346` is dead code. Task's suggested fix: give the DELETE an *older* cutoff than the UPDATE so expired rows persist for at least one full cleanup cycle before removal. Note this same UPDATE-then-DELETE-same-cutoff pattern likely also exists for the teams_outbox/session_relay cleanup blocks further down in `_cleanup_once` (visible in the surrounding code but not called out in the task description) — worth a quick check whether the task's AC #4 is scoped to `messages` only or expects the same discipline applied wherever it recurs; if scope is ambiguous, that's a "stop and ask" scope-change moment per `backlog instructions task-execution`, not a silent expansion.
3. Also relevant but NOT itself an acceptance criterion: the task description calls out that sub-bug 3 is "compounded" by `hook.py`'s decision-polling loop (`hook.py:174-192`) — `elapsed += chunk` credits the full chunk toward the hook's own timeout budget regardless of how long `await_decision` actually blocked, so a few early/lost-race returns (sub-bug 3) can burn through `CRM_DECISION_TIMEOUT` far faster than intended, falling through to the `"ask"` fallback prematurely. Fixing sub-bug 3 alone should substantially mitigate this (each `await_decision` call will then actually block for its full requested chunk unless genuinely ready), but re-read `hook.py`'s loop once sub-bug 3 is fixed to confirm the compounding is resolved — it's context, not a listed AC, so don't expand scope to "fix hook.py" without checking with the user first if something there still looks broken.
4. Acceptance criteria: #1 (broadcast wakes identity-scoped waiters), #2 (Notifier event map bounded), #3 (lost-race wait_for continues for remaining timeout), #4 (cleanup sweep doesn't delete-what-it-just-marked-expired, so expired is observable), #5 (a test covers the broadcast-wakeup path specifically) — all five need genuine before/after behavioral evidence (e.g. a test that would fail on unfixed code), not just code presence.
5. This is a **reliability/store-labeled** task touching the shared long-poll primitive every blocking tool in the codebase depends on (`wait_for_instruction`, `wait_for_completion`, `await_decision`, `subscribe`, `wait_for_pending_approval`, `wait_for_pending_session_ops`, `wait_for_pending_teams_send`, ...) — higher blast radius than FMC-4's isolated validation-layer fix. Run the FULL test suite after each sub-bug, not just the store tests, and budget extra time for an independent adversarial-review subagent pass on the branch diff before opening the PR (same discipline as FMC-4, arguably more warranted here).
6. Continue the lifecycle: tracker update on branch (advance cursor to FMC-6, move FMC-5 to Resolved, session-log entry) → commit → **`git status --porcelain` check immediately before `gh pr merge`** (this discipline has caught real bugs before; keep doing it every session) → adversarial review (`git diff dev...HEAD`) → push → PR → merge → **verify `origin/dev`'s log actually contains every commit you made** → prune → re-arm.

## Critical context / traps

- **This repo's campaign default branch is `dev`, not `main`** — same as every prior session; `main` is a separate downstream branch this campaign does not touch unless asked.
- **`gh pr merge --delete-branch` deletes the LOCAL feature branch too**, not just the remote one, when run while that branch is checked out — confirmed this session (`git branch -d feature/FMC-4` failed with "branch not found" because `gh` had already removed it as part of the merge). Don't be alarmed if step 10's local `git branch -d` errors this way; it means pruning already happened, not that something went wrong. Verify with `git branch --list` and `git ls-remote --heads origin feature/<KEY>` before treating it as an anomaly.
- **The `git status --porcelain` check immediately before `gh pr merge` (not just before the last commit) has not recurred as a bug since sessions 3/4** — keep doing it every session regardless of how the session "feels."
- FMC-5's 4 sub-bugs are independent to fix, but sub-bugs 1, 2, and 3 all touch the same `Notifier` class (`services/store.py:128-177`) — consider whether fixing them together in one pass (rather than three separate edits that each re-read the class) reduces the chance of one fix's edit clobbering another's. Sub-bug 4 (`_cleanup_once`) is in a different method and can be done independently.
- Sub-bug 3's fix changes `Notifier.wait_for`'s control flow, which EVERY long-poll tool in the codebase calls through — after fixing it, sanity-check that a genuine timeout (no wakeup ever happens) still returns `None` at the correct total elapsed time, not early and not hung forever.
- FMC-6 (next in queue after FMC-5, and the last item) is Low severity and about tool-pattern deviations from CLAUDE.md's documented contract — lower risk, not yet explored this session.

## Do not repeat

- Don't batch a `backlog task edit` (or any file mutation) with a `git commit` in the same parallel tool-call round unless you've explicitly staged that exact file in that exact commit — verify with `git status --porcelain` immediately before committing, not just before staging.
- Don't run `backlog task edit --append-notes` (e.g. to record a post-review finding) as an afterthought once the branch is already pushed and the PR is about to be merged — fold that note into the same commit/push cycle as the review itself, before triggering the merge, so there's no window for a dangling uncommitted edit.
- Don't trust a single grep pass when renaming/fixing a string or pattern that recurs in multiple places — re-grep broadly for every instance of a pattern being changed (e.g. FMC-5's UPDATE-then-DELETE-same-cutoff pattern in `_cleanup_once` may recur for teams_outbox/session_relay beyond the `messages` table the task calls out explicitly) before considering a fix complete.
- Don't assume a bug-shaped task necessarily needs a code fix without checking exploitability/impact first when the task allows for a no-op outcome — FMC-5 does NOT have such a branch (all 4 sub-bugs are described as straightforwardly real, with exact line numbers and existing dead-code evidence like `STATUS_EXPIRED` never being reachable), so this is a straight-fix task like FMC-2/FMC-3, not a verify-first task like FMC-8 was.
- Don't expand scope silently if the UPDATE-then-DELETE-same-cutoff pattern turns out to recur beyond what AC #4 literally describes (teams_outbox/session_relay cleanup blocks) — stop and ask the user whether to fold it into this task or file separate follow-up work, per `backlog instructions task-execution`'s scope-change rule.
