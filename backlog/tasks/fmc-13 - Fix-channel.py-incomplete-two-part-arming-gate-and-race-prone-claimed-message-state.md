---
id: FMC-13
title: >-
  Fix channel.py: incomplete two-part arming gate and race-prone claimed-message
  state
status: To Do
assignee: []
created_date: '2026-07-21 14:44'
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
- [ ] #1 A channel sidecar instance with channel_enabled or CHANNEL_ENABLED set to true, but that was not actually launched by Claude Code with the --dangerously-load-development-channels flag, does not announce itself as channel-capable and does not begin claiming, pushing, or permission-relaying mesh messages; the arming decision incorporates an actual signal that development channels loaded for this session rather than only the local enabled setting, and a timeout waiting for that signal results in staying disarmed rather than proceeding to arm anyway.
- [ ] #2 A reply that fails to relay to the mesh (the relay call errors or returns an unsuccessful result) does not cause the inbox loop to treat the claimed message as consumed; the loop only advances past a message once its reply has actually been recorded on the mesh.
- [ ] #3 Once the inbox loop has given up on a claimed message and sent a non-consumption bounce for it, a genuine reply the agent submits afterward for that same message is no longer silently discarded behind a generic, indistinguishable warning; the agent instead receives a response that clearly indicates the message was already finalized by a non-consumption bounce, distinguishable from an ordinary invalid or unknown message id error.
- [ ] #4 If pushing a claimed message to the agent fails (for example, a broken stdio pipe), the claimed-message state is reliably cleared rather than persisting indefinitely; and while any claimed-message state is active, it is never applied to a permission request that does not actually belong to that in-flight turn, so a later, unrelated permission request (including the operator's own genuine local work) is not evaluated against a stale message's triggering_admin metadata and correctly falls through to the local terminal permission dialog.
- [ ] #5 Automated tests cover the arming-gate signal check, the failed-mesh-reply-does-not-advance-the-loop case, the late-reply-after-bounce case, and the push-failure-clears-claimed-state case, each demonstrated to fail against the pre-fix code and pass against the fix.
<!-- AC:END -->
