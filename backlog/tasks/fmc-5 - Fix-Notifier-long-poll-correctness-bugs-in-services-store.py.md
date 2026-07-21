---
id: FMC-5
title: Fix Notifier/long-poll correctness bugs in services/store.py
status: Done
assignee:
  - '@claude'
created_date: '2026-07-20 20:25'
updated_date: '2026-07-21 09:00'
labels:
  - reliability
  - store
dependencies: []
priority: medium
type: bug
ordinal: 5000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by an ad-hoc agent-team dogfooding review (2026-07-20) of services/store.py's Notifier and cleanup sweep.

1. Broadcast messages never wake identity-scoped waiters (store.py:253-255). enqueue_message(recipient_session=None) only notifies the inbox:* key, but pop_next_for_worker("foo") does return NULL-recipient rows (store.py:286-291). A worker long-polling on inbox:foo therefore sleeps the full poll_max_wait_s before seeing a broadcast meant for anyone. test_storage.py:131 only covers the pop path, not the notify path, so this is untested.

2. Notifier._events is never pruned (store.py:136-143). Keys include outbox:{message_id}, approval:{approval_id}, teams_outbox:{id}, session_relay:{id} — one permanent asyncio.Event per item ever waited on, in a process meant to run under pm2 indefinitely. Slow unbounded memory leak.

3. wait_for re-checks exactly once on wakeup (store.py:177). If check() returns None because another waiter already claimed the row, wait_for returns None immediately instead of continuing to wait for the remainder of the caller's timeout. This is compounded at hook.py:192, where elapsed += chunk credits the full chunk regardless of how long await_decision actually blocked — a few early returns can burn through CRM_DECISION_TIMEOUT in seconds and fall through to the "ask" fallback well before the intended timeout.

4. STATUS_EXPIRED is unobservable because the cleanup sweep deletes what it just marked (store.py:882-890). _cleanup_once first marks queued/delivered rows as expired where created_at < cutoff, then immediately deletes rows in (replied, cancelled, expired) using the same cutoff, in the same sweep. Every row the UPDATE just touched gets removed by the DELETE. So a waiter never actually observes status: "expired" — it just gets NotFoundError (messaging.py:112) — and the STATUS_EXPIRED branch at store.py:346 is dead code. Fix: give the DELETE an older cutoff than the UPDATE so expired rows persist for at least one cleanup cycle before removal.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Broadcast messages (no specific recipient) wake identity-scoped long-poll waiters immediately instead of only after the poll timeout elapses
- [x] #2 The Notifier event map does not grow without bound over a long-running server process
- [x] #3 A long-poll that loses a race on wakeup continues waiting for the remainder of the original timeout instead of returning early
- [x] #4 The cleanup sweep no longer deletes expired rows in the same pass that marks them expired, so callers can observe the expired status
- [x] #5 A test covers the broadcast-wakeup path
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Notifier (store.py:128-177) — rewrite together since sub-bugs 1/2/3 share it:
   - notify_prefix(prefix): notify every currently-registered key starting with
     prefix (fixes AC#1 combined with enqueue_message change below).
   - forget(key): drop a key's Event from _events unless a waiter is currently
     parked on it (tracked via a new _waiters refcount dict) — safe eviction
     hook for AC#2.
   - wait_for(): track a monotonic deadline and loop re-checking/re-waiting on
     the (possibly notify()-replaced) event until the deadline is exhausted,
     instead of returning after the first post-wakeup check() (fixes AC#3).
     Increment/decrement _waiters[key] around the wait loop (try/finally).
2. enqueue_message (store.py:228-256): on a genuine broadcast
   (recipient_session is None), call notify_prefix("inbox:") instead of only
   notifying the wildcard key, so every identity-scoped long-poll waiter wakes
   too (pop_next_for_worker already returns NULL-recipient rows to them).
   Addressed-message notify path unchanged.
3. _cleanup_once (store.py:885+) — messages table only: capture ids via a
   SELECT before each DELETE, and give the messages DELETE an
   `messages_delete_cutoff = cutoff - delete_grace` so a row marked expired
   this sweep is deleted only in a LATER sweep (fixes AC#4). Confirmed via
   existing tests (test_teams_outbox.py / test_session_relay.py
   test_cleanup_removes_stale_pending) that teams_outbox/session_relay must
   KEEP same-pass mark+delete — do not touch those blocks' cutoff semantics,
   only add id-capture for notifier eviction. After the transaction commits,
   call self._notifier.forget() for every id deleted this sweep across
   messages/approvals/teams_outbox/session_relay (outbox:/approval:/
   teams_outbox:/session_relay: keys) — completes AC#2's eviction path.
   inbox:<session> keys are never forgotten (bounded by live identities, not
   per-message).
4. _periodic_cleanup: hoist the sweep interval into a variable and pass it as
   delete_grace to _cleanup_once so production sweeps get the real one-cycle
   grace period; direct test calls to _cleanup_once keep defaulting
   delete_grace=0 (unchanged legacy behavior) unless a test passes it
   explicitly.
5. Tests (tests/test_storage.py): broadcast-wakes-identity-scoped-waiter
   (AC#5), Notifier.forget skips an actively-waited key (safety unit test),
   wait_for continues after a lost race (AC#3 unit test against Notifier
   directly), expired message survives one cleanup cycle before deletion then
   is pruned on the next (AC#4), cleanup evicts a resolved message's outbox
   notifier key (AC#2). Run full `uv run pytest` + `uv run ruff check` after.
6. hook.py's elapsed+=chunk loop (mentioned in the task as compounded by
   sub-bug 3) is context only, not a listed AC — re-read after the Notifier
   fix to confirm the compounding is resolved, but do not modify hook.py
   unless something there is still actually broken.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Rewrote Notifier (store.py) together for sub-bugs 1-3 since they share the class:
notify_prefix(prefix) wakes every currently-registered key under a prefix; forget(key)
evicts a resolved key's Event unless a waiter refcount (new _waiters dict) shows someone
still parked on it; wait_for() now loops on a monotonic deadline, re-checking and
re-fetching the (notify()-replaced) event after each wakeup instead of returning after
one post-wakeup check.

Sub-bug 1: enqueue_message's broadcast branch (recipient_session is None) now calls
notify_prefix("inbox:") instead of only notifying the wildcard inbox:* key -- every
identity-scoped waiter can claim a NULL-recipient row via pop_next_for_worker, so all
must wake, not just wildcard listeners. Addressed-message notify path unchanged.

Sub-bug 2: _cleanup_once now SELECTs ids before each DELETE (messages/approvals/
teams_outbox/session_relay) and calls notifier.forget() on outbox:/approval:/
teams_outbox:/session_relay: keys for every row actually deleted that sweep.
inbox:<session> keys are never evicted (bounded by live identities, not per-message).

Sub-bug 3: wait_for's re-check loop covered by a Notifier-level regression test
(lost-race continues waiting) and a genuine-timeout-still-returns-None sanity test.

Sub-bug 4: _cleanup_once gained a delete_grace parameter (default 0.0, preserving
legacy same-pass behavior for direct callers) -- messages_delete_cutoff = cutoff -
delete_grace, so a row marked expired this sweep survives to the next sweep before
deletion. _periodic_cleanup passes the real sweep interval as delete_grace. Confirmed
via existing test_teams_outbox.py/test_session_relay.py test_cleanup_removes_stale_pending
that teams_outbox/session_relay must KEEP same-pass mark+delete (their own ~30min
request timeouts are far shorter than the 7-day store TTL, so nothing is realistically
still waiting by cleanup time) -- did not touch those blocks' cutoff semantics, only
added id-capture for notifier eviction. This resolved the scope-ambiguity flagged in
the handover without needing to ask: the existing passing tests are authoritative that
the UPDATE-then-DELETE-same-cutoff pattern there is intentional, not a copy of the bug.

Verification: git-stashed only store.py (kept the new tests) and confirmed all 5 new
regression tests fail against the pre-fix code, then restored the fix and confirmed all
pass. Full suite: `uv run pytest` 284 passed (up from 277, +7 new tests -- 5 regression
tests plus 2 extra Notifier sanity tests). `uv run ruff check src/ tests/` clean.

hook.py's elapsed+=chunk loop (mentioned in the task as compounded by sub-bug 3) was
re-read after the fix: await_decision now genuinely blocks the full requested chunk on
a lost race, so the compounding is resolved by the Notifier fix alone -- left hook.py
untouched per the task's own note that this is context, not a listed AC.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed all 4 Notifier/long-poll correctness bugs in services/store.py. (1) Broadcast messages now wake every identity-scoped long-poll waiter via a new Notifier.notify_prefix(), not just the wildcard inbox:* key. (2) Notifier._events no longer grows unbounded: a new forget() method (guarded by a waiter refcount so it never evicts a key someone is actively parked on) is wired into _cleanup_once, which now captures deleted message/approval/teams_outbox/session_relay ids and evicts their notifier keys each sweep. (3) wait_for() now loops on a monotonic deadline, continuing to wait out the remaining timeout after a lost-race wakeup instead of returning early. (4) _cleanup_once gained a delete_grace parameter so a message marked expired this sweep is only pruned in a later sweep, making status=expired observable to wait_for_completion() callers instead of an opaque NotFoundError; confirmed via existing tests that teams_outbox/session_relay intentionally keep same-pass mark+delete (their timeouts are far shorter than the store TTL) so left those untouched. Added 7 tests to tests/test_storage.py (5 regression + 2 sanity), each independently confirmed to fail against the pre-fix code via git stash and pass against the fix. Verified: uv run pytest (284 passed, up from 277), uv run ruff check src/ tests/ (clean). All 5 ACs checked with objective before/after evidence.
<!-- SECTION:FINAL_SUMMARY:END -->
