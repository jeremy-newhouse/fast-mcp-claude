---
id: FMC-9
title: >-
  Fix channel.py: forgeable triggering_admin/operator_direct trust stamps in the
  permission relay and send_teams
status: To Do
assignee: []
created_date: '2026-07-21 14:44'
labels:
  - security
  - channel
dependencies: []
references:
  - backlog/docs/reviews/doc-2 - Codex-full-codebase-review-2026-07-21.md
priority: high
type: bug
ordinal: 9000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
A second-opinion review by OpenAI Codex (gpt-5.6-sol, ultra reasoning effort) audited the full codebase on 2026-07-21. Both findings below were independently re-verified against the current code in this same session (file and approximate line cited, behavior traced through the real call path) before being written up here. The full adjudicated report is saved as Backlog document doc-2, "Codex full-codebase review (2026-07-21)", if broader context is ever needed, but this description is self-contained.

Both bugs live in the same trust chain: send_prompt's caller-supplied metadata argument (tools/messaging.py) flows unchecked into the channel sidecar's in-flight message state (channel.py), where two different handlers treat message-shape signals as proof of admin or operator authority, even though this server's whole mesh authenticates with exactly one shared bearer credential, MCP_API_KEY. There is no code path anywhere that distinguishes a trusted hub or admin peer from any other peer holding that same key.

Bug 1 (critical): forgeable triggering_admin auto-allows tool calls with no human in the loop.

send_prompt (tools/messaging.py) accepts an arbitrary metadata dict from the caller. It is only validated for JSON size (validate_metadata calls validate_json_object_size, see utils/validation.py around lines 211-215) -- there is no allowlist of permitted keys and no check on who the caller is. That metadata is stored verbatim with the message and handed to the channel sidecar as the in-flight message's metadata once a session claims it.

channel.py's permission-request handler, _handle_permission, around lines 1417-1427 (the actual gate is the check at line 1425), auto-allows the pending tool call whenever metadata.triggering_admin is exactly true on a message addressed to that identity (recipient_session matches the identity). Because every peer shares the identical bearer key, any bearer-authenticated peer -- not just a trusted hub or admin -- can call send_prompt addressed at a known channel identity (identities are trivially discoverable via the who tool) with metadata equal to triggering_admin: true, and every Bash, Edit, Write, or other tool call in that pushed turn gets auto-allowed with zero human confirmation. This defeats the entire purpose of the admin fast-path in the permission relay.

Scoping note, so this is not overstated: when there is no in-flight message at all (inflight is None), the handler does NOT auto-allow anything -- it deliberately falls through to Claude Code's own local terminal permission dialog, so a human still has to click allow in that case. The exposure is specifically the triggering_admin-stamped code path once an addressed message is in flight; it is not a blanket bypass of all permission checking.

This is a distinct trust-boundary gap from the already-Done FMC-2 (who() leaking announce_token, which was about forging an identity claim). Here the caller's identity is not in question -- the gap is that any legitimately-identified caller can stamp an arbitrary authority level onto a message it sends to someone else's channel identity, and the receiving side has no way to verify that stamp came from a trusted origin rather than from the message's own sender.

Bug 2 (critical, narrower than a blanket bypass): send_teams's operator_direct trust stamp can be attributed to a remote-originated turn.

channel.py's send_teams handler, _handle_send_teams, around lines 1123-1136, stamps operator_direct: true onto its request to the hub whenever there is no in-flight message in the runtime state (rt.inflight is None), treating the absence of in-flight state as proof that the local human operator is directly driving the session and personally typed this request. The hub honors operator_direct as one of exactly two trusted origins for that call (the other being an addressed, admin-triggered in-flight task).

The problem: "no in-flight message" does not reliably mean "the operator just typed this." Two ways it can be wrong, both in the inbox loop around lines 832-866:

First, FYI-classified inbound messages (hub-stamped fire-and-forget turns -- session-relay notifications, broadcasts, late-reply push-backs, identified by metadata.expects_reply being false) are deliberately pushed to the session WITHOUT setting rt.inflight at all, specifically so an unanswered FYI cannot wedge the mailbox for the reply timeout (30 minutes by default). If the agent, while acting on a remote-originated FYI's content, calls send_teams, the handler sees no in-flight state and stamps operator_direct: true exactly as if the local human had typed the request themselves.

Second, rt.inflight is unconditionally cleared in a finally block right after awaiting consumption, including on the ambiguous or unknown verdict path where the code explicitly does not bounce because it cannot rule out the original turn still genuinely executing and a late reply still landing. So there is a window, right after that clear and before the next message is claimed, where a send_teams call is still conceptually part of the prior remote-originated turn but gets the same operator_direct treatment as fresh local input.

Because send_teams's own tool-call dispatch is unconditionally allowed by the permission relay (it is in the sidecar's always-allow list of its own tools, so the relay never gates the call itself), this operator_direct trust stamp is the only gate standing between "this Teams post came from a remote-originated context" and "the hub attributes it as if the local operator personally asked for it."

Both bugs share the same underlying design gap: the channel sidecar infers authority (admin, or local-operator) from message-shape signals -- a caller-supplied metadata key in bug 1, or merely the absence of in-flight state in bug 2 -- rather than from anything structurally tied to who actually originated the action, and the single shared bearer key gives the receiving side no independent way to check either inference.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A bearer-authenticated peer that is not a designated trusted hub or admin origin can no longer get its pushed tool calls auto-allowed by the channel permission relay by setting metadata.triggering_admin to true on a send_prompt call addressed to a known channel identity; the case where no message is currently in flight continues to fall through to the local terminal permission dialog unchanged.
- [ ] #2 The channel sidecar's send_teams handler no longer grants the operator-direct trust level (the stamp the hub treats as proof the local human operator personally requested the post) during a window where the current turn is not actually a genuine local-operator-typed prompt -- specifically while an FYI-classified inbound message is being acted on, and immediately after in-flight state is cleared following an ambiguous or unknown consumption outcome where the originating turn may still be executing.
- [ ] #3 Both fixes are covered by tests that demonstrate the vulnerable behavior would fail without the fix and pass with it: one test showing an addressed send_prompt carrying metadata.triggering_admin=true from an untrusted caller does not result in the channel permission relay auto-allowing a subsequent tool call, and one test showing a send_teams call made during FYI processing or in the post-ambiguous-clear window does not receive operator-direct trust.
<!-- AC:END -->
