---
id: FMC-5
title: Fix Notifier/long-poll correctness bugs in services/store.py
status: To Do
assignee: []
created_date: '2026-07-20 20:25'
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
- [ ] #1 Broadcast messages (no specific recipient) wake identity-scoped long-poll waiters immediately instead of only after the poll timeout elapses
- [ ] #2 The Notifier event map does not grow without bound over a long-running server process
- [ ] #3 A long-poll that loses a race on wakeup continues waiting for the remainder of the original timeout instead of returning early
- [ ] #4 The cleanup sweep no longer deletes expired rows in the same pass that marks them expired, so callers can observe the expired status
- [ ] #5 A test covers the broadcast-wakeup path
<!-- AC:END -->
