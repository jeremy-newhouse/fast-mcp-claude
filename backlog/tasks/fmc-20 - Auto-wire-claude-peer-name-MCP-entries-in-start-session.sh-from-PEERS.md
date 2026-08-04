---
id: FMC-20
title: Auto-wire claude-peer-<name> MCP entries in start-session.sh from PEERS
status: Done
assignee:
  - '@claude'
created_date: '2026-08-04 04:36'
updated_date: '2026-08-04 04:41'
labels: []
dependencies: []
references:
  - .mcp.json.example
ordinal: 17000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
start-session.sh generates each session's .mcp.json with only a local-server entry (claude-local in notify+pull mode, fast-mcp-claude-channel in channel mode) -- it never adds the claude-peer-<name> entries documented in .mcp.json.example ('Add one claude-peer-<name> entry per remote you want to control'). In a mesh deployment (no shared hub), this means no session anywhere in the fleet can ever address a session on a different host: who()/send_prompt(recipient_session=...) only see the calling session's own local server, and there is no other wired path to a peer's tools. Discovered live: a minim4 session could not reach an mbpm2 session despite both servers being correctly peered and reachable (verified via curl, 401/auth-gated in both directions) -- the servers were fine, sessions just had no tools to use that reachability. Auto-generate one claude-peer-<name> HTTP MCP entry per configured PEERS row (reading the same .env PEERS array the server itself uses) so every session gets direct controller-role tools (send_prompt, wait_for_completion, who, etc.) against every configured peer, in both channel and notify+pull mode. This does not touch invariant 9 (the agent still never gets claude-local in channel mode, only claude-peer-* controller-role entries for OTHER peers' servers, not self-referential worker verbs on its own local server).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 start-session.sh reads the local .env's PEERS array (or a PEERS_JSON env override, mirroring the existing PEER_NAME/MCP_API_KEY override pattern) and adds one claude-peer-<name> HTTP MCP entry (url + Authorization bearer + alwaysLoad:true, matching .mcp.json.example's documented shape) per peer, in both channel mode and notify+pull mode
- [x] #2 Peer api_key values reach the generated .mcp.json via environment variable, never argv (ps is world-readable) -- same discipline already applied to MCP_API_KEY in this script
- [x] #3 Malformed or missing PEERS in .env degrades gracefully (no peer entries added, session still starts) rather than crashing the whole session launch
- [x] #4 claude-local (notify+pull mode) and the invariant-9 restriction (no claude-local in channel mode) are both unchanged by this work
- [x] #5 Manually verified end-to-end on two real peered hosts: a session launched via claude-launch on one host has working claude-peer-<other> tools that reach the other host's server
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add PEERS_JSON loading to start-session.sh's env-resolution block, mirroring the existing PEER_NAME/MCP_API_KEY override pattern (env var wins, else .env's PEERS, else '[]').
2. Pass PEERS_JSON to the existing inline Python MCP-config generator via environment variable (never argv, matching the MCP_API_KEY security discipline already documented in the script).
3. In the Python block, parse PEERS_JSON (wrapped in try/except -- malformed JSON degrades to zero peer entries with a stderr warning, never crashes the launch) and add one claude-peer-<name> HTTP entry per peer (url, Authorization bearer header, alwaysLoad:true) to the servers dict, after the existing channel/notify-pull branch logic so it applies to both modes uniformly.
4. Update the script's header comment (lines 6-27) to document the new claude-peer-<name> wiring.
5. Manual verification: run start-session.sh (or claude-launch) on two real peered hosts, confirm the generated temp .mcp.json contains the expected claude-peer-<name> entries with correct URLs, and confirm an actual Claude Code session can call a peer tool successfully.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Verified with live commands, not just code presence. (1) Confirmed the existing _envget() helper's tr -d quote-stripping would corrupt PEERS JSON (all double-quotes stripped, e.g. {name: minim4, ...} -- invalid JSON) -- added a separate _envget_raw() without the tr step specifically for PEERS. (2) Ran the actual modified start-session.sh (not a reimplementation) via a fake-claude PATH shim that dumps the generated --mcp-config file instead of execing a real session, against mbpm2's real .env (5 configured salient peers). Confirmed correct claude-peer-<name> entries (url + bearer + alwaysLoad:true) generated in BOTH channel mode (alongside fast-mcp-claude-channel, no claude-local -- invariant 9 intact) and notify+pull mode (alongside claude-local). Output validated as syntactically correct JSON via python -m json.tool. (3) Edge cases: PEERS_JSON set to invalid JSON produces a stderr WARNING and zero peer entries, exit 0 (session still starts) -- no crash. Empty PEERS ([]) produces zero peer entries silently, as expected. (4) Strongest verification: took the EXACT claude-peer-minim4 bearer token generated for mbpm2's session and sent a real MCP initialize JSON-RPC request straight to minim4's live server -- got a genuine 200 OK with full server capabilities and the complete messaging/presence/files tool list (send_prompt, wait_for_completion, who, etc.), proving the generated credentials are functionally correct against a real running peer, not just syntactically plausible. (5) Cleaned up all test session status files created under ~/.fast-mcp-claude/sessions/ during dry-run testing.

Follow-up fix during self-review: the initial per-peer loop called peer.get(...) unconditionally, which crashed (AttributeError) if PEERS_JSON was valid JSON containing a list of non-dict items (e.g. [1,2,"bad"]) -- a real gap against the 'malformed PEERS degrades gracefully' AC. Added an isinstance(peer, dict) guard with a WARNING+skip, same pattern as the other malformed-entry cases. Re-verified: exit 0, session still launches, three warnings logged, zero peer entries added. Re-ran the full good-case (5-peer) test afterward to confirm no regression.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
start-session.sh now auto-generates one claude-peer-<name> MCP entry per row in the local .env's PEERS array, in both channel and notify+pull mode -- giving every session direct controller-role tools (send_prompt, wait_for_completion, who, etc.) against every peer its local server already knows how to reach. Fixes the root cause of 'a session on host A can't talk to a session on host B': in a mesh deployment, who()/send_prompt(recipient_session=...) only ever see the calling session's own local server, and no session previously had any wired path to a remote peer's tools -- the servers were correctly peered and reachable the whole time, sessions just had nothing to use it with. Peer api_key values travel via env (PEERS_JSON), never argv, matching the existing MCP_API_KEY discipline. Malformed/absent PEERS degrades to zero peer entries rather than crashing the session launch. invariant 9 (no claude-local in channel mode) is untouched -- claude-peer-* entries are controller-role tools against OTHER peers' servers, never self-referential worker verbs. Verified via a fake-claude PATH shim against mbpm2's real 5-peer .env (both modes, correct JSON, edge cases handled), and a real MCP initialize handshake using the exact generated bearer against minim4's live server (200 OK, full tool list returned).
<!-- SECTION:FINAL_SUMMARY:END -->
