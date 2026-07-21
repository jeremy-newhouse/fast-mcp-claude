---
id: FMC-12
title: >-
  Fix store.py hub-drain races: double-claimed teams/session-relay sends and
  unbounded Notifier growth via adversarial identities/channels
status: Done
assignee:
  - '@jeremy'
created_date: '2026-07-21 14:44'
updated_date: '2026-07-21 18:44'
labels:
  - reliability
  - store
dependencies: []
references:
  - backlog/docs/reviews/doc-2 - Codex-full-codebase-review-2026-07-21.md
priority: high
type: bug
ordinal: 12000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by a second-opinion full-codebase review (OpenAI Codex, gpt-5.6-sol, ultra effort, 2026-07-21), independently re-verified against the actual current code in this session (file/line quoted, behavior traced). Both sub-bugs live in services/store.py and are folded into one task per this project's convention of bundling same-file findings from one review pass.

1. Non-atomic hub-drain claims let a Teams post or a session-relay operation be performed twice (HIGH, security/correctness).

list_pending_teams_sends (store.py around lines 572-579) and list_pending_session_ops (store.py around lines 663-670) are plain SELECT statements with no claim or lock step. Compare this to pop_next_for_worker (store.py around line 307 onward), which is the atomic pattern this same file otherwise uses: it takes the _db_lock, does the SELECT, and immediately UPDATEs the row's status to delivered before releasing the lock and returning, so a second caller racing in cannot see the same row as still pending. list_pending_teams_sends and list_pending_session_ops skip that step entirely: they read rows with status equal to the pending constant and return them as-is, without marking them claimed.

complete_teams_send (store.py around lines 593-608) and complete_session_op (store.py around lines 684-706) only guard the completion WRITE: the UPDATE is conditioned on the row's current status still being pending (WHERE id=? AND status=pending), so only the first completion call for a given row actually updates it and returns true; a second call for the same id returns false. But that guard only protects the write that records the outcome -- it does nothing to prevent two hub-side drainers from having already both read the same pending row via wait_for_pending_teams_sends or wait_for_pending_session_ops and both gone on to perform the real-world side effect the row represents.

Concretely: if two concurrent hub-side drain loops both call wait_for_pending_teams_sends (or both call wait_for_pending_session_ops) around the same time -- for example, two hub worker tasks, or a retry/reconnect racing an in-flight drain -- both can observe the same pending row in their SELECT before either has completed it. Both then proceed to perform the external action the row represents: for teams_outbox, posting the message text to the target Teams chat; for session_relay, routing/executing the requested cross-peer operation (list sessions, or send a message to another peer session). Only one of the two complete_teams_send/complete_session_op calls will succeed (whichever runs first wins the status=pending guard); the other returns false and its caller presumably treats that as a completion failure. But by that point the side effect already happened twice: a Teams message can be posted twice into a real chat, or a session-relay send can be delivered/executed twice to another peer session. The write-side guard prevents double-bookkeeping but not the double side effect, because the claim never happened atomically with the read.

2. Unbounded Notifier memory growth via adversarially-chosen recipient_session/channel values (HIGH, security -- memory-growth denial of service).

Notifier._get (store.py around lines 143-148) unconditionally creates and stores a new asyncio.Event for any key it is asked about that doesn't already have one, including when the caller's wait timeout is zero or negative (the event still gets created before the timeout is evaluated). Notifier.forget(), the eviction method delivered by the already-completed task FMC-5, is only ever invoked by the periodic cleanup sweep, and only for keys tied to database rows that were actually deleted that sweep (outbox:, approval:, teams_outbox:, session_relay: prefixed keys). FMC-5's own implementation notes explicitly recorded the assumption that inbox: and pubsub: prefixed keys are never forgotten because they are "bounded by live identities, not per-message" -- i.e., the fix assumed the set of distinct inbox/pubsub keys that could ever be created is small and tied to real, finite identities/channels.

That assumption does not hold. wait_for_instruction's recipient_session argument (tools/messaging.py) is passed only through validate_session_id, and subscribe's channel argument (tools/pubsub.py) is passed only through validate_channel (utils/validation.py) -- both are pure format/regex checks (matching SESSION_RE / CHANNEL_RE respectively) with no check that the identity or channel actually corresponds to any real, live presence row or existing pubsub history. Any authenticated caller can therefore invoke wait_for_instruction with a syntactically valid but entirely made-up recipient_session (e.g. a fresh random string matching the session-id pattern), or subscribe with a syntactically valid but nonexistent channel name, passing timeout=0 each time so the call returns immediately. Each such call runs Notifier._get on a brand-new inbox:<made-up-value> or pubsub:<made-up-channel> key, permanently allocating one more asyncio.Event in the in-process _events dict. Because these keys are never tied to a database row that cleanup can delete, and because the periodic cleanup sweep never touches inbox:/pubsub: keys at all (by FMC-5's explicit design), none of these entries are ever forgotten. An authenticated caller (or a compromised/malicious peer holding a valid API key) can repeat this with an unbounded number of distinct fabricated identity/channel values, growing the Notifier's memory footprint without bound for as long as the server process runs under pm2 -- a slow but unbounded memory-growth denial-of-service vector.

This is a gap in FMC-5's fix, not a duplicate of anything FMC-5 already resolved: FMC-5 fixed unbounded growth for message/approval/teams-outbox/session-relay-keyed Notifier entries (all tied to real, deletable database rows), but its fix explicitly assumed inbox:/pubsub: keys were safe to leave unforgotten because they're bounded by live identities -- an assumption this finding shows is false once you account for adversarially-chosen, non-existent identity/channel values that were never validated for existence, only for format.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Two concurrent hub-side drain calls (wait_for_pending_teams_sends or list_pending_teams_sends) can no longer both observe the same pending teams_outbox row as claimable; the read that surfaces a pending row to a caller atomically marks it as claimed (e.g. transitions its status away from pending) so a second concurrent drainer sees it as already taken and does not re-perform the Teams-post side effect for that row.
- [x] #2 Two concurrent hub-side drain calls (wait_for_pending_session_ops or list_pending_session_ops) can no longer both observe the same pending session_relay row as claimable; the read that surfaces a pending row to a caller atomically marks it as claimed so a second concurrent drainer does not re-perform (re-route/re-execute) the session-relay operation for that row.
- [x] #3 An authenticated caller repeatedly invoking wait_for_instruction with fresh, syntactically-valid but nonexistent recipient_session values (or subscribe with fresh, syntactically-valid but nonexistent channel values), each with a zero or near-zero timeout, does not cause the Notifier's in-memory event map to grow without bound over the life of the server process -- entries for identities/channels with no corresponding live presence row or real backing data are eventually evicted or otherwise bounded.
- [x] #4 Both fixes (the atomic-claim fix for teams_outbox/session_relay draining, and the Notifier growth bound for adversarial inbox:/pubsub: keys) are covered by new automated tests that fail against the pre-fix code and pass against the fix, and the full existing test suite continues to pass alongside them.
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add OUTBOX_CLAIMED status constant (pending -> claimed -> done) to services/store.py.
2. Sub-bug 1: make list_pending_teams_sends/list_pending_session_ops atomically
   claim (SELECT+UPDATE under _db_lock, mirroring pop_next_for_worker) instead of
   a bare SELECT, so two concurrent hub drain calls can never both see the same
   pending row. Widen complete_teams_send/complete_session_op's completion guard
   from status=pending to status IN (pending, claimed). Extend _cleanup_once's
   stale-expiry sweep to also catch CLAIMED rows (a drain that claimed then
   crashed before completing must not dangle forever).
3. Sub-bug 2: Notifier.wait_for skips creating/registering an Event entirely when
   timeout<=0 (the cheap, unlimited-rate adversarial-key vector); Notifier._events
   becomes an LRU-ordered OrderedDict capped at max_events (default 10_000),
   evicting least-recently-used keys with zero active waiters once over capacity
   (never evicting a key someone is actively parked on, same guard as forget()).
4. Add regression tests for both sub-bugs in tests/test_teams_outbox.py,
   tests/test_session_relay.py, and tests/test_storage.py; update the two existing
   round-trip tests whose bare-pending assertion is now claimed-on-read.
5. Verify via git stash (store.py only) that every new/changed test fails against
   the pre-fix code and passes after; run the full suite + ruff.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Verified all 4 ACs with objective evidence, each confirmed via git stash (store.py
only) to fail against pre-fix code and pass post-fix:

AC#1/#2 (atomic drain claims): test_concurrent_drain_claims_row_exactly_once
(test_teams_outbox.py) and test_concurrent_drain_claims_op_exactly_once
(test_session_relay.py) run two concurrent list_pending_*() calls via
asyncio.gather against a single pending row and assert the row surfaces exactly
once combined. Also independently reproduced the raw race with a standalone
script against pre-fix store.py: both concurrent calls returned the SAME row id
(['f6f57e73...', 'f6f57e73...']), confirming the exact double-claim described in
the task. Post-fix: exactly one call claims it (pending -> claimed under
_db_lock, mirroring pop_next_for_worker); complete_teams_send/complete_session_op
widened to accept status IN (pending, claimed) so direct-completion callers
(existing tests) still work. Also added test_cleanup_removes_stale_claimed for
both queues: a row claimed then never completed (simulated hub crash) is now
caught by _cleanup_once's stale-expiry sweep instead of dangling forever.

AC#3 (Notifier growth): 5 new tests in test_storage.py. wait_for() now
short-circuits before ever calling _get() when timeout<=0, so the cheap
unlimited-rate zero-timeout attack (via wait_for_instruction/subscribe with a
fabricated recipient_session/channel) leaks nothing --
test_zero_timeout_wait_for_does_not_leak_notifier_event,
test_wait_for_instruction_zero_timeout_does_not_leak_notifier_events, and
test_subscribe_zero_timeout_does_not_leak_notifier_events exercise this at the
Notifier, Store, and real-tool-path (wait_for_next_for_worker/wait_for_pubsub)
levels respectively. For the general case (nonzero-timeout bursts), _events is
now an LRU-ordered OrderedDict capped at max_events (default 10_000): once over
capacity, least-recently-used keys with zero active waiters are evicted --
test_notifier_events_bounded_by_max_events_cap confirms the cap holds, and
test_notifier_eviction_never_evicts_an_active_waiter confirms a real in-flight
waiter's key is never evicted regardless of capacity pressure (same invariant
FMC-5's forget() already established).

AC#4 (test coverage + full suite): full suite 326 passed (up from 315, +11 new
tests: 2 concurrency, 2 stale-claimed cleanup, 5 Notifier growth-bound, 2
existing round-trip assertions updated from bare-pending to claimed-on-read).
uv run ruff check src/ tests/ clean. ruff format flags the same pre-existing
drift class already documented in FMC-4/6/8/14/11 (confirmed store.py's drift
predates this branch via git show dev:... | ruff format --check); none of my
touched files (store.py aside, which was already drifting) are newly flagged.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed both store.py bugs from the codex review. (1) list_pending_teams_sends/
list_pending_session_ops now atomically claim (pending -> claimed, under
_db_lock) instead of a bare SELECT, mirroring pop_next_for_worker's
select+update pattern -- two concurrent hub drain calls can no longer both
observe (and both act on) the same row. complete_teams_send/complete_session_op
widened to accept status IN (pending, claimed); _cleanup_once's stale-expiry
sweep now also catches CLAIMED rows so a crashed-mid-drain row doesn't dangle
forever. (2) Notifier.wait_for skips registering an Event at all when
timeout<=0, closing the cheap/unlimited-rate fabricated-identity DoS vector;
Notifier._events is now an LRU-capped OrderedDict (default 10_000) that evicts
least-recently-used, non-actively-waited keys once over capacity, bounding
growth generally while never evicting a key a live long-poll is parked on.
Added 11 tests (326 total, up from 315); every new/changed assertion confirmed
via git stash to fail against the pre-fix code (including a standalone script
reproducing the literal double-claim) and pass against the fix. Full suite
passes; ruff check clean; ruff format's pre-existing drift on store.py (and
other files) confirmed to predate this branch, per the FMC-4/6/8/14/11
precedent. All 4 ACs checked.
<!-- SECTION:FINAL_SUMMARY:END -->
