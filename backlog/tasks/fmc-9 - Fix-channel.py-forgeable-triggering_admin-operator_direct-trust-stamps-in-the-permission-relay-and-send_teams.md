---
id: FMC-9
title: >-
  Fix channel.py: forgeable triggering_admin/operator_direct trust stamps in the
  permission relay and send_teams
status: Done
assignee:
  - '@claude'
created_date: '2026-07-21 14:44'
updated_date: '2026-07-21 19:18'
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
- [x] #1 A bearer-authenticated peer that is not a designated trusted hub or admin origin can no longer get its pushed tool calls auto-allowed by the channel permission relay by setting metadata.triggering_admin to true on a send_prompt call addressed to a known channel identity; the case where no message is currently in flight continues to fall through to the local terminal permission dialog unchanged.
- [x] #2 The channel sidecar's send_teams handler no longer grants the operator-direct trust level (the stamp the hub treats as proof the local human operator personally requested the post) during a window where the current turn is not actually a genuine local-operator-typed prompt -- specifically while an FYI-classified inbound message is being acted on, and immediately after in-flight state is cleared following an ambiguous or unknown consumption outcome where the originating turn may still be executing.
- [x] #3 Both fixes are covered by tests that demonstrate the vulnerable behavior would fail without the fix and pass with it: one test showing an addressed send_prompt carrying metadata.triggering_admin=true from an untrusted caller does not result in the channel permission relay auto-allowing a subsequent tool call, and one test showing a send_teams call made during FYI processing or in the post-ambiguous-clear window does not receive operator-direct trust.
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Root cause: this server has exactly ONE shared inbound bearer (MCP_API_KEY) -- 
auth.py's ApiKeyVerifier returns the same generic AccessToken for every caller,
so there is no way to structurally distinguish a trusted hub/admin origin from
any other peer holding the same key. Both bugs are symptoms of channel.py
trusting message-shape signals (caller metadata; absence of in-flight state)
instead of anything tied to verified origin.

Bug 1 fix (AC#1) -- move the trust decision server-side (candidate #3 from the
task description), narrowly:
1. config.py: add optional Settings.mcp_admin_api_key (default None). Unset =>
   nobody is ever admin-trusted (closes the hole by default); an operator can
   provision a distinct admin credential to a genuinely trusted hub process.
2. auth.py: ApiKeyVerifier gains an optional admin_api_key param; verify_token
   checks the general key first (unchanged fast/happy path), then the admin
   key if configured, returning AccessToken(claims={"admin": True}) only on an
   admin-key match. Preserve existing rate-limiter/lockout semantics.
3. server.py: build_auth_provider passes settings.mcp_admin_api_key through.
4. tools/messaging.py send_prompt: after validate_metadata, if the caller's
   metadata dict contains "triggering_admin", clamp it to
   caller_value AND is_admin (via fastmcp.server.dependencies.get_access_token()
   claims) -- never force it True, only ever allowed to stay True when the
   caller actually authenticated with the admin key. channel.py's existing
   _handle_permission logic (trust meta.get("triggering_admin") is True and
   addressed) is UNCHANGED -- it becomes safe because the stored metadata is
   now authoritative instead of caller-forgeable.
5. Docs: .env.example, README security section, CLAUDE.md channel-push-flow
   section -- document MCP_ADMIN_API_KEY and that triggering_admin is now
   server-verified, not caller-supplied.

Bug 2 fix (AC#2) -- entirely internal to channel.py, no auth changes:
1. Add _Runtime.remote_turn_started_ts: float | None, a module constant
   _REMOTE_CONTEXT_GRACE_S, and three small helpers: _mark_remote_context(rt),
   _clear_remote_context(rt), _remote_context_active(rt) (elapsed-since-mark <
   grace).
2. _inbox_loop's FYI branch calls _mark_remote_context(rt) around the push (an
   FYI's content may still be getting acted on with no in-flight slot held).
3. After _await_consumption: verdict == _UNKNOWN -> _mark_remote_context(rt)
   (the original turn may still be executing); CONSUMED/DEAD ->
   _clear_remote_context(rt) (turn is genuinely over).
4. _handle_send_teams: when inflight is None, only stamp operator_direct=True
   if NOT _remote_context_active(rt); otherwise stamp neither trust flag
   (matches the hub's existing fail-safe refusal of untrusted metadata).

Tests (AC#3), each confirmed via git stash to fail pre-fix / pass post-fix:
- test_auth.py: admin-key match yields claims={"admin": True}; general-key
  match does not; wrong key still fails/rate-limits as before.
- test_messaging.py (or a new test_messaging_admin.py): a REAL HTTP server +
  real ApiKeyVerifier(general, admin) + real fastmcp.Client (mirrors
  test_hook.py's relay_server pattern, needed because get_access_token()
  requires genuine request context) -- (a) a non-admin-authenticated
  send_prompt with metadata.triggering_admin=true addressed to a recipient
  stores triggering_admin=False, and feeding that stored message into
  channel_mod._handle_permission proves it does NOT auto-allow a subsequent
  tool call; (b) an admin-authenticated caller's send_prompt stores
  triggering_admin=True and DOES auto-allow (positive control).
- test_channel.py: reuse the existing _run_inbox/_ScriptedClient harness --
  (a) the FYI push path leaves remote_context_active() true, and a
  send_teams call in that state gets neither trust flag; (b) an UNKNOWN
  verdict leaves remote_context_active() true with the same effect; (c) a
  DEAD verdict clears it; (d) after the grace window elapses (monkeypatch
  channel_mod.time.monotonic), operator_direct recovers to True.

Update CLAUDE.md's Channel push flow section for both behavior changes.
Full suite + ruff check before finalizing.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Root cause: this server has exactly one shared inbound bearer (MCP_API_KEY); auth.py's
ApiKeyVerifier returned the same generic AccessToken for every caller, so nothing
distinguished a trusted hub/admin origin from any other mesh peer holding the key.

Bug 1 fix: added an optional, distinct Settings.mcp_admin_api_key. auth.py's ApiKeyVerifier
now accepts it and stamps AccessToken.claims={"admin": True} ONLY on a match against that
key (unset by default -> nobody is ever admin-trusted). tools/messaging.py::send_prompt
clamps a caller-supplied metadata["triggering_admin"] to (caller_value AND is_admin) via
fastmcp.server.dependencies.get_access_token() -- never forces it True, only allows it to
stay True when the request actually authenticated with the admin key. channel.py's
_handle_permission is UNCHANGED: it already trusted the stored metadata verbatim, which is
now safe since the value can no longer be forged upstream.

Bug 2 fix (entirely internal to channel.py, no auth changes): added
_Runtime.remote_turn_started_ts + _mark_remote_context/_clear_remote_context/
_remote_context_active (60s grace constant _REMOTE_CONTEXT_GRACE_S). _inbox_loop marks it
when pushing an FYI (expects_reply=false) and when _await_consumption returns _UNKNOWN
(ambiguous -- the original turn may still be executing); clears it on _CONSUMED/_DEAD.
_handle_send_teams only stamps operator_direct=true when inflight is None AND the grace
window is not active; otherwise it stamps neither trust flag (the hub's existing logic
already fails safe and refuses when neither flag is present).

Verification: 14 new tests added (327 -> 341 total), all confirmed via `git stash` of the
5 changed src/ files to FAIL against the pre-fix code and PASS against the fix:
- tests/test_auth.py: TestApiKeyVerifierAdminKey (5 tests) -- admin key yields
  claims={"admin": True}; general key does not; wrong key still rejected/rate-limited.
- tests/test_send_prompt_admin_trust.py (new file, 3 tests): a REAL HTTP server + real
  ApiKeyVerifier(general, admin) + real fastmcp.Client (mirrors test_hook.py's pattern,
  required because get_access_token() needs genuine request context) -- a non-admin
  send_prompt with metadata.triggering_admin=true stores triggering_admin=False AND, fed
  into the real channel_mod._handle_permission, does NOT auto-allow a subsequent tool call
  (routes to Teams instead); an admin-authenticated caller's send_prompt stores
  triggering_admin=True and DOES auto-allow (positive control, proves the fix narrows
  rather than disables the admin fast-path).
- tests/test_channel.py (5 new tests, reusing the existing _run_inbox/_ScriptedClient
  harness): FYI push marks the remote-context window active; an _UNKNOWN consumption
  verdict marks it active; a _DEAD verdict clears it; send_teams stamps neither trust flag
  while the window is active; send_teams recovers operator_direct=true once the window
  genuinely elapses.
Full suite: `uv run pytest` 341 passed (up from 327). `uv run ruff check src/ tests/`
clean. `uv run ruff format --diff` confirmed my new/changed lines introduce ZERO new
formatting drift beyond the pre-existing drift already on dev in channel.py/test_channel.py
(same class documented in FMC-4/6/8/11/12 sessions) -- verified by diffing each hunk's line
range against my actual edits.
Docs updated: .env.example (MCP_ADMIN_API_KEY), README.md Security section, CLAUDE.md
Channel push flow + Security model sections.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed both critical trust-boundary bugs. Bug 1: added a distinct, optional
MCP_ADMIN_API_KEY (auth.py's ApiKeyVerifier stamps AccessToken.claims={"admin": True} only
on a match) and made send_prompt (tools/messaging.py) clamp a caller's
metadata.triggering_admin claim to that server-verified truth instead of trusting it
verbatim -- closing the forgeable-admin-stamp auto-allow path while leaving channel.py's
existing trust-the-stored-metadata logic and the no-in-flight local-dialog fallback
unchanged. Bug 2: added a bounded remote-context grace window in channel.py
(_mark_remote_context/_clear_remote_context/_remote_context_active) so send_teams no
longer treats "no in-flight message" as proof of operator-direct trust while an
unanswered FYI may still be getting acted on, or immediately after an ambiguous
consumption verdict clears in-flight state. Verified with 14 new tests (341 total, up
from 327), each independently confirmed via git stash to fail against the pre-fix code
and pass against the fix; full suite and ruff check clean; my changes introduce no new
formatting drift beyond dev's pre-existing drift in the touched files. Updated
.env.example, README.md, and CLAUDE.md to document MCP_ADMIN_API_KEY and the new
server-verified trust model. All 3 ACs checked with objective evidence.
<!-- SECTION:FINAL_SUMMARY:END -->
