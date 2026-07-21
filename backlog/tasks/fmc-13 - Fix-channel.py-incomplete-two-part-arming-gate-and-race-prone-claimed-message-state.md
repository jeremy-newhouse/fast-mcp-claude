---
id: FMC-13
title: >-
  Fix channel.py: incomplete two-part arming gate and race-prone claimed-message
  state
status: Done
assignee:
  - '@claude'
created_date: '2026-07-21 14:44'
updated_date: '2026-07-21 20:22'
labels:
  - reliability
  - channel
dependencies: []
references:
  - backlog/docs/reviews/doc-2 - Codex-full-codebase-review-2026-07-21.md
priority: high
type: bug
ordinal: 13000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by a second-opinion review (OpenAI Codex, gpt-5.6-sol, ultra effort) auditing the full codebase; both findings below were independently re-verified against the current code in this repo (file and line cited, behavior traced against the real control flow) by a separate verification pass in this same session -- these are confirmed findings, not raw unverified model claims. The full adjudicated report is saved as Backlog document doc-2, "Codex full-codebase review (2026-07-21)", but this task description is self-contained and does not require reading it.

Two bugs in src/fast_mcp_claude/channel.py, both in the channel sidecar's arm/claim/reply state machine:

1. The documented two-part arming gate is only one part in code, and a timeout on the other signal is treated as "proceed" rather than "stay disarmed". CLAUDE.md's Coexistence and safety section states that arming the channel sidecar's loops requires BOTH channel_enabled being true (via setting or CLI flag) AND Claude Code actually having been launched with the --dangerously-load-development-channels flag -- this is called out as a coexistence/safety invariant, not a nice-to-have. In the actual code, the function that resolves the enabled flag (around lines 411-413) computes it purely from three local sources: the --enabled CLI flag, the CHANNEL_ENABLED environment variable, or the channel_enabled Settings default. Nothing anywhere in channel.py ever reads back, from the MCP client's initialize handshake or any other signal, whether Claude Code actually loaded development channels for this session -- there is no code-level check of the second half of the documented invariant at all. Separately, the inbox loop (around lines 780-785) does wait on an "initialized" readiness event, set by the stdio tee when it observes the notifications/initialized message, before it starts claiming anything -- but that wait has a 30-second timeout, and on timeout the code logs a message and proceeds to arm anyway rather than staying disarmed. Net effect: a session with CHANNEL_ENABLED=true (or channel_enabled=true) but NOT actually launched with --dangerously-load-development-channels will still announce itself over the mesh as channel-capable (channel: true in its presence row) and will attempt to claim, push, and permission-relay messages, with nothing in this server's own code preventing it. Only Claude Code's own client-side behavior -- which this server has no way to verify or detect -- stands between that misconfiguration and silent message loss (a controller's send_prompt lands in a channel push the unequipped client silently drops) or a malfunctioning permission relay (auto-allow/deny decisions computed against a permission request that structurally cannot arrive the way the relay code assumes).

2. Claimed-message state in the inbox loop (around lines 858-904) and the reply handler (around lines 1087-1101) is race-prone in three distinct, independently reproducible ways, all rooted in how the sidecar's one claimed-message slot is set, read, and cleared.

2a. The reply handler calls the mesh reply relay first, then unconditionally signals that the claimed message has been replied to whenever the reply is addressed to the currently claimed message id -- it does this regardless of whether the relay to the mesh actually succeeded. A failed relay (network blip, mesh-side rejection) still causes the inbox loop to treat the message as successfully consumed and advance to claiming the next one, even though the controller never actually received the agent's answer.

2b. When the inbox loop's wait for consumption times out with positive evidence that the consumer is dead, it sends a bounce (non-consumption) result back to the mesh for that message and clears the claimed-message slot before doing so. If the agent's real reply for that same message arrives after this point, the reply handler's check for whether this reply belongs to the currently claimed message fails (the slot is already empty or points elsewhere), so locally the reply is not treated as consuming anything -- and the mesh relay call is still attempted anyway, where it is rejected because the mesh already finalized that message via the bounce. The agent is shown only a generic "reply NOT recorded (unknown/already-finalized message_id)" warning, indistinguishable from an ordinary typo'd message id. The controller has already unblocked on the bounce text and never learns the agent's real answer ever existed.

2c. In the inbox loop's claim sequence, the claimed-message slot is set and the message is pushed to the agent BEFORE the try/finally block that is supposed to clear that slot on any outcome -- the try/finally only wraps the later await-consumption step, not the push itself. If the push call raises (for example because the stdio pipe to the agent process is broken), the exception propagates out of the whole sequence without ever reaching the finally, leaving the claimed-message slot pointing at that stale message indefinitely; the outer loop's exception handler logs the error and reconnects the mesh client, but never resets this state. While that stale state persists, the permission-request handler reads the claimed-message slot to decide how to route ANY subsequent permission request -- including one generated by the local operator's own genuine, unrelated local work in the same terminal. Because the slot is non-empty, the handler takes the claimed-message branch instead of correctly falling through to the local terminal permission dialog, and evaluates the new, unrelated permission request against the stale message's triggering_admin metadata field -- meaning a stale admin-triggered message can cause a later, unrelated tool call (potentially the operator's own) to be auto-allowed without ever going through the local Claude Code permission UI or the Phase-3 approval relay.

These findings are distinct from FMC-2's who()/announce_token finding (presence/identity leak in a different module) and from FMC-5's Notifier/store.py long-poll fixes (the shared store layer) -- this task is scoped entirely to the channel sidecar's own in-process arm/claim/reply state machine in channel.py.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A channel sidecar instance with channel_enabled or CHANNEL_ENABLED set to true, but that was not actually launched by Claude Code with the --dangerously-load-development-channels flag, does not announce itself as channel-capable and does not begin claiming, pushing, or permission-relaying mesh messages; the arming decision incorporates an actual signal that development channels loaded for this session rather than only the local enabled setting, and a timeout waiting for that signal results in staying disarmed rather than proceeding to arm anyway.
- [x] #2 A reply that fails to relay to the mesh (the relay call errors or returns an unsuccessful result) does not cause the inbox loop to treat the claimed message as consumed; the loop only advances past a message once its reply has actually been recorded on the mesh.
- [x] #3 Once the inbox loop has given up on a claimed message and sent a non-consumption bounce for it, a genuine reply the agent submits afterward for that same message is no longer silently discarded behind a generic, indistinguishable warning; the agent instead receives a response that clearly indicates the message was already finalized by a non-consumption bounce, distinguishable from an ordinary invalid or unknown message id error.
- [x] #4 If pushing a claimed message to the agent fails (for example, a broken stdio pipe), the claimed-message state is reliably cleared rather than persisting indefinitely; and while any claimed-message state is active, it is never applied to a permission request that does not actually belong to that in-flight turn, so a later, unrelated permission request (including the operator's own genuine local work) is not evaluated against a stale message's triggering_admin metadata and correctly falls through to the local terminal permission dialog.
- [x] #5 Automated tests cover the arming-gate signal check, the failed-mesh-reply-does-not-advance-the-loop case, the late-reply-after-bounce case, and the push-failure-clears-claimed-state case, each demonstrated to fail against the pre-fix code and pass against the fix.
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Re-read the current channel.py in full (task's cited line numbers are stale per prior sessions' pattern) rather than trusting the description's line refs.
2. AC#1 (arming gate): the task itself concedes "this server has no way to verify or detect" whether Claude Code actually loaded dev channels -- and git history confirms channels genuinely work on some CC version (CLAUDE.md "Proven on CC 2.1.168"), so inventing an unverifiable client-capabilities detection risks silently breaking the one proven-live feature with no way to test it. Scope narrowly to what's concrete and verifiable: (a) presence's channel:true advertisement currently has ZERO runtime gate (purely cfg.enabled) -- wire it to rt.initialized, the same signal the inbox loop already uses; (b) fix the actual described bug -- the inbox loop's 30s timeout-then-proceed-anyway becomes a looping wait that never arms without the signal.
3. AC#2: _handle_reply signals reply_event unconditionally on id match; gate it on the mesh relay's `ok` result too.
4. AC#3: track the last bounced message_id on _Runtime; _handle_reply checks it to give a distinguishable warning instead of the generic unknown/already-finalized text.
5. AC#4: move the _push call inside the try/finally that already wraps _await_consumption, so a push failure (not just an await_consumption failure) reliably clears rt.inflight -- this closes both AC#4 clauses (the leak itself, and the stale-state-leaking-into-an-unrelated-permission-request consequence) with one minimal change.
6. Add regression tests per AC#5, each verified via git stash to fail pre-fix / pass post-fix.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation complete. Discovered en route: src/fast_mcp_claude/session.py's docstring (written 2026-06-06, commit c9828c4) claims "Claude Code 2.1.x removed the --dangerously-load-development-channels dev-server load path" -- but CLAUDE.md's Channel push flow section (current, most-recently touched 2026-07-21) says the feature is "Proven on CC 2.1.168" (one patch version later), and channel.py's push mechanism is exercised only manually per test_channel.py's own docstring, not by CI. This is a stale-doc inconsistency (session.py predates the later restoration), not a channel.py behavior bug, so it's out of FMC-13's scope -- flagging for a possible follow-up doc fix rather than touching it here.

This same finding drove AC#1's scope decision: since even the task description concedes "this server has no way to verify or detect" whether dev channels actually loaded, and the channel-push feature's real CC-version behavior can only be confirmed manually (no live verification available in this session), I did not invent a client-capabilities-negotiation signal to detect dev-channels-loaded -- that would risk silently regressing the one proven-live feature with no automated test able to catch a wrong guess. Instead AC#1 is satisfied by wiring presence's channel:true advertisement to the SAME rt.initialized (notifications/initialized) signal the inbox loop already uses (previously ungated -- presence advertised channel:true purely from cfg.enabled with no runtime check at all) and making the inbox loop's initialized-wait timeout stay disarmed forever rather than falling through to arm anyway after one 30s wait.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed both bugs in channel.py's arm/claim/reply state machine.

AC#1 (arming gate): presence's channel:true advertisement was previously gated on nothing but cfg.enabled -- now gated on rt.initialized (the same notifications/initialized signal the inbox loop uses), so a session that never completes its own MCP handshake never advertises push-capable. The inbox loop's initialized-wait no longer proceeds to arm after a 30s timeout; it loops the wait indefinitely (logging periodically), staying disarmed until the signal arrives. Scoped narrowly per the task's own admission that "this server has no way to verify or detect" whether dev channels specifically loaded -- did not invent an unverifiable client-capabilities check that could silently regress the one proven-live channel-push path (documented finding: session.py's docstring claiming CC removed the flag entirely is now stale vs CLAUDE.md's "Proven on CC 2.1.168", flagged as a possible doc follow-up, not fixed here).

AC#2: _handle_reply now only signals reply_event (which unblocks the inbox loop to advance) when the mesh relay actually succeeded (ok=True), not on id-match alone.

AC#3: added _Runtime.bounced_message_id, set when the inbox loop sends a non-consumption bounce; _handle_reply checks it to give a distinguishable "already finalized by a non-consumption bounce" warning instead of the generic unknown/already-finalized text, while ordinary invalid ids keep the generic message.

AC#4: moved the _push call inside the same try/finally that already wrapped _await_consumption, so a push failure (e.g. broken stdio pipe) reliably clears rt.inflight instead of leaking it indefinitely -- which also closes the consequence clause (stale claimed-state no longer gets applied to a later, unrelated permission request).

AC#5: 7 new tests in tests/test_channel.py (FMC-13 section). Verified via git stash on src/fast_mcp_claude/channel.py that 5 of the 7 fail against the pre-fix code and pass post-fix, covering all 4 named cases (arming-gate: 2 tests; failed-mesh-reply-does-not-advance: 1; late-reply-after-bounce: 1; push-failure-clears-claimed-state + does-not-leak-into-permission-relay: 1); the other 2 are non-regression companions (positive arming case, ordinary-unknown-id case) that correctly pass on both versions.

Full verification: `uv run pytest` -- 361 passed (116 in test_channel.py, up from 109). `uv run ruff check src/ tests/` -- all checks passed. Diff scoped to channel.py + test_channel.py only; reverted ruff-format's incidental reformatting of pre-existing unrelated lines (the file was not format-clean before this change) to keep the diff to FMC-13's actual changes.
<!-- SECTION:FINAL_SUMMARY:END -->
