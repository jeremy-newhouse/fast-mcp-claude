---
id: FMC-15
title: >-
  Fix launcher.py: duplicate-instance ownership is unenforced, and in-flight
  replies are lost across reconnect and shutdown
status: In Progress
assignee:
  - '@jeremy'
created_date: '2026-07-21 14:44'
updated_date: '2026-07-21 21:09'
labels:
  - reliability
  - launcher
dependencies: []
references:
  - backlog/docs/reviews/doc-2 - Codex-full-codebase-review-2026-07-21.md
priority: high
type: bug
ordinal: 15000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by a second-opinion review (OpenAI Codex, gpt-5.6-sol, ultra effort) of the full codebase (2026-07-21); every sub-bug below was independently re-verified against the current code in this same session (file and line cited, behavior traced through the actual control flow) -- these are confirmed, not raw unverified review output.

All three bugs live in src/fast_mcp_claude/launcher.py. The launcher has an ECA-71 owner-token mechanism (announce_token, stamped into every announce call) that is supposed to let the server detect a second process trying to claim the same launcher identity, but the launcher-side consumer of that signal is a dead end, and separately its stale-claim reaper and its always-reply guarantee do not actually hold up across a client reconnect or a shutdown signal.

1. Duplicate-instance detection is logged but never enforced, the reaper runs before this process's own ownership is confirmed, and the poll loop claims work unconditionally. The heartbeat loop (around lines 995 to 1008) calls announce() every cfg.heartbeat seconds; when the server refuses with error code IDENTITY_LIVE_ELSEWHERE (another live process already owns this identity's announce_token), the handler only logs a message once (via a refusal_logged flag) and keeps looping -- it never sets anything the bridge or poll loop reads, never stops the bridge, never stops claiming. Separately, the stale-claim reaper (_reap_stale_claims) runs at the top of the connect block in _bridge (around lines 1088 to 1099), guarded only by a reaped_once flag, immediately after the client connection opens and BEFORE the heartbeat task is even created -- meaning before this process's own first announce call has gone out on this connection. So the very first thing a second, accidentally-started launcher process does on connecting is list every 'delivered' message addressed to its own identity and reply launcher_restarted_task_lost to each one, even though those rows may be the FIRST (legitimate) launcher's genuinely in-flight tasks -- the in-flight tracking set lives only in the first process's memory, invisible to the second. After that reap, the poll loop's inner while-loop (around lines 1109 to 1136) acquires a semaphore slot and calls wait_for_instruction with no check anywhere of whether this process's own announce ever actually succeeded before it starts pulling and claiming new messages from the identity's mailbox. Concrete failure scenario: an operator botches a restart (a supervisor script starts a replacement launcher before the old one has exited, or pm2 briefly runs two instances during a deploy). The second, illegitimate instance does not stop itself despite the server explicitly telling it via IDENTITY_LIVE_ELSEWHERE that another live instance already owns the identity -- instead it silently fails the real owner's genuinely in-progress tasks out from under it (each reaped row flips away from 'delivered', so the real worker's eventual real reply later hits NOT_REPLIABLE and is lost) and then begins competing for and claiming brand-new work from the same mailbox, so two claude -p processes race on the same logical task stream. This matters because the whole point of the announce_token guard is to let the server detect exactly this situation, but nothing on the launcher side actually acts on the detection.

2. A task handler's captured client connection goes stale on reconnect, and its reply is silently lost. Each claimed message is handled by an asyncio task created as asyncio.create_task(_handle_task(c, msg, cfg, sem, live, running, inflight)) around line 1133 -- the client object c from the CURRENT connection is captured directly in that closure and used for every call inside the handler, including the final reply. When the bridge's heartbeat loop detects a dead session and signals a reconnect, or a transport error occurs, the outer exception handlers in _bridge (around lines 1140 to 1153) cancel only heartbeat_task and then loop back around to open a brand-new client connection -- they never touch the running task-handler set or cancel any handler still executing against the OLD, now-closing client. A handler that is mid-run (for example still waiting on the spawned claude -p subprocess) keeps running to completion and then tries to reply using the closure-captured old client object. The reply function retries three times with backoff (starting around line 808) and then just logs a failure line and returns (around line 818) -- the reply is never delivered anywhere else. Meanwhile, on the very same reconnect, the bridge explicitly SKIPS the stale-claim reaper, with an inline comment at line 1101 noting that live in-flight tasks must not be reaped, precisely because this task's message id is still present in the in-flight tracking set. The result: the task is simultaneously ineligible for reaping (because it looks live to the code that decides that) and permanently unable to deliver its reply (because its only usable connection object is already dead), with no logic anywhere that detects this specific stuck state and either retries the reply on the new connection or makes the row reapable again. The message sits stuck until the mesh's own message time-to-live expires it (documented elsewhere in this codebase as a multi-day TTL), and the controller that originally sent the task via send_prompt/wait_for_completion simply hangs until then with no useful signal. Concrete failure scenario: a transient network blip, or a fast-mcp-claude server restart, happens while a launcher is mid-task; the task itself finishes normally, but its reply is silently swallowed, and the waiting controller gets nothing back for potentially days.

3. Cancellation during shutdown tears down the client connection before the always-reply shutdown sweep runs, so that sweep silently fails against an already-closed client. The bridge's main loop lives inside an async-with block around the client connection (line 1084: async with Client(cfg.local_url, **client_kwargs) as c). When the process receives SIGTERM or SIGINT, _serve's wait-for-stop call returns and cancels the bridge task. That cancellation is raised at whatever point the bridge task is currently suspended -- in the steady state this is almost always INSIDE the async-with block (blocked in the long-poll, or awaiting a claimed task) -- and as the exception propagates upward it first unwinds through the client's own context-manager exit, which tears down that connection as part of normal exit, and only THEN reaches the outer except-CancelledError clause (around line 1137), which calls the shutdown routine with that same now-defunct client object. The shutdown routine (_shutdown, lines 1156 to 1194) is the one place documented in the module's own header docstring (the ALWAYS-REPLY invariant: 'Every claimed message gets EXACTLY ONE reply() on EVERY exit path ... launcher shutdown') as responsible for replying to every still-in-flight task on shutdown -- but by the time it runs, the connection it is handed has already had its context manager exit. Inside _shutdown, the list-messages call and every reply attempt that follows are wrapped in one broad try/except (around lines 1178 to 1193) that catches any exception and only logs 'shutdown reply sweep failed', so when the torn-down connection fails these calls, no reply is sent for any task that was genuinely in flight at shutdown time -- the launcher logs the failure and exits anyway, printing a shutdown-complete line that undercounts (or fully zeroes out) how many in-flight tasks actually got replied to. Concrete failure scenario: an operator restarts or stops the launcher (pm2 restart, a deploy, a manual SIGTERM) while one or more tasks are still running; none of their controllers receive a launcher_shutdown reply, so each hangs on wait_for_completion until the underlying message's own TTL expires, and the mesh never learns what happened to those tasks. This directly defeats the module's own documented guarantee that every claimed message always gets exactly one reply, including on launcher shutdown.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A launcher process that receives an IDENTITY_LIVE_ELSEWHERE rejection from announce (meaning another live process already owns this identity) stops competing for and claiming work under that identity, instead of merely logging the rejection once and continuing to poll and claim exactly as before.
- [x] #2 The stale-claim reaper never runs, and the poll loop never claims new work, until this process's own ownership of the identity has actually been confirmed by a successful announce on the current connection -- so a duplicate/illegitimate launcher instance cannot reap the real owner's genuinely in-flight tasks nor start claiming new mailbox work.
- [x] #3 A task claimed before a client reconnect (transport blip, detected dead session, or local server restart) still results in its reply being delivered to the mesh after the reconnect completes, instead of the reply attempt silently failing against the now-closed pre-reconnect connection and the task sitting unresolved until the message's time-to-live expires.
- [x] #4 Every task that is genuinely in flight when the launcher receives a shutdown signal (SIGTERM or SIGINT) results in a launcher_shutdown reply actually delivered to the mesh, even though the process is being cancelled -- instead of the shutdown reply sweep silently failing because cancellation already tore down the client connection it depends on.
- [x] #5 Regression tests cover all three scenarios (a rogue second instance under IDENTITY_LIVE_ELSEWHERE no longer reaps/claims, a task's reply survives a mid-task client reconnect, and a task's reply survives a shutdown-time cancellation), with each test demonstrated to fail against the pre-fix code and pass against the fix.
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. ECA-71 owner-token gate (AC#1/#2): add owner_confirmed: asyncio.Event to _heartbeat_loop
   (set on successful announce, cleared on any non-success incl. IDENTITY_LIVE_ELSEWHERE).
   Add _wait_for_owner_confirmed_or_reconnect() helper (races owner_confirmed against
   reconnect_needed, mirroring _wait_for_instruction_or_reconnect, to avoid deadlocking if
   the heartbeat exits before ever confirming). _bridge awaits this gate before the first
   reap AND re-checks it every poll-loop iteration.
2. Reconnect-safe replies (AC#3): add _ClientBox (mutable holder for the bridge's CURRENT
   connection, survives reconnects). _bridge passes the box (not the raw client) to
   _handle_task. _send_reply re-reads client_source.client on every retry attempt when
   given a box, and retry budget widened (_REPLY_RETRY_ATTEMPTS/_REPLY_RETRY_BACKOFF_S) to
   outlast a typical heartbeat-detected reconnect.
3. Shutdown-safe replies (AC#4): _shutdown no longer reuses _bridge's (already torn-down-by-
   cancellation) connection; it opens its OWN fresh Client for the list_messages + reply
   sweep. Signature changes from _shutdown(client, cfg, ...) to
   _shutdown(cfg, client_kwargs, ...).
4. Regression tests (AC#5): test_owner_token_refused_never_reaps_or_claims,
   test_handle_task_reply_survives_reconnect, test_shutdown_uses_fresh_connection_for_reply_sweep
   — each confirmed via git stash to fail against pre-fix launcher.py and pass post-fix.
5. Prior art: mirrors channel.py's ECA-71 announce_confirmed gate (Layer B) for AC#1/#2,
   adapted to launcher.py's single-connection _bridge structure (vs channel.py's split
   presence/inbox connections) — gate is per-connection per AC#2's own wording ("confirmed
   ... on the current connection").
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation complete on feature/FMC-15. Full diff touches only src/fast_mcp_claude/launcher.py
(+ tests/test_launcher.py). Summary of the fix per bug:

AC#1/#2 (owner-token gate): _heartbeat_loop now takes an owner_confirmed: asyncio.Event, set on
a successful announce and cleared on any non-success (most notably IDENTITY_LIVE_ELSEWHERE).
New helper _wait_for_owner_confirmed_or_reconnect races owner_confirmed against reconnect_needed
(same shape as the existing _wait_for_instruction_or_reconnect) so the gate can't deadlock if the
heartbeat exits before ever confirming. _bridge awaits this gate before running the reaper AND
re-checks it on every poll-loop iteration (a later mid-run refusal also stops new claims, not
just the very first reap). Design mirrors channel.py's ECA-71 announce_confirmed (Layer B),
adapted to launcher.py's single shared connection (channel.py splits presence/inbox onto separate
connections) -- gated per-connection to match AC#2's own wording ("confirmed ... on the current
connection").

AC#3 (reconnect-safe reply): new _ClientBox holds _bridge's CURRENT connection and survives
reconnects (unlike the raw client). _bridge now passes the box (not the raw client `c`) into
_handle_task, and _send_reply re-reads client_source.client on every retry attempt when given a
box -- so a reply started against a connection that later closes picks up whatever connection is
CURRENT instead of exhausting retries against the dead one. Retry budget widened
(_REPLY_RETRY_ATTEMPTS=8, _REPLY_RETRY_BACKOFF_S=1.0, module-level so tests can shrink them) to
outlast a typical heartbeat-detected reconnect.

AC#4 (shutdown-safe reply): _shutdown no longer reuses _bridge's connection (already torn down by
the time cancellation reaches _shutdown, since __aexit__ runs before the except-CancelledError
clause). It now opens its OWN fresh Client for the list_messages + reply sweep. Signature changed
from _shutdown(client, cfg, live, tasks, heartbeat_task) to
_shutdown(cfg, client_kwargs, live, tasks, heartbeat_task).

AC#5 (regression tests): three new tests in tests/test_launcher.py --
test_owner_token_refused_never_reaps_or_claims, test_handle_task_reply_survives_reconnect,
test_shutdown_uses_fresh_connection_for_reply_sweep. Verified via `git stash` (stashing only
launcher.py, keeping the tests) that all three FAIL against pre-fix code (AttributeError /
'called list_messages while unconfirmed' / '0 replies delivered'), then pass again after
`git stash pop`.

Verification: `uv run pytest` -- 365 passed (was 362; +3 new, 2 pre-existing heartbeat-loop call
sites updated for the new owner_confirmed param, no others broken). `uv run ruff check src/
tests/` -- all checks passed. `uv run ruff format --check` flags 9 files including launcher.py/
test_launcher.py, but confirmed via `git stash` that this format drift PRE-EXISTS on dev
(unrelated to this change) -- left untouched to avoid unrelated churn.
<!-- SECTION:NOTES:END -->
