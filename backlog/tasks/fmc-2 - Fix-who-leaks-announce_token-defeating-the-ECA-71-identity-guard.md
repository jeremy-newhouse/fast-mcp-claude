---
id: FMC-2
title: 'Fix: who() leaks announce_token, defeating the ECA-71 identity guard'
status: To Do
assignee: []
created_date: '2026-07-20 20:25'
labels:
  - security
  - presence
dependencies: []
priority: high
type: bug
ordinal: 2000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by an ad-hoc agent-team dogfooding review (2026-07-20) of src/fast_mcp_claude/ against CLAUDE.md's documented security model.

`_row_to_presence` (services/store.py:1017-1024) returns a presence row's `metadata` verbatim, and both `channel.py:515` and `launcher.py:967` put `announce_token` into that metadata. Any caller of `who()` (presence.py:121) therefore reads every live session's own identity/mailbox-ownership token.

Attack chain: `who()` -> harvest peer X's token -> `forget(X, token)` (presence.py:84) -> `announce(X, {announce_token: mine})` -> attacker now owns X's mailbox, and X's own sidecar gets `IDENTITY_LIVE_ELSEWHERE` and disarms its inbox loop (channel.py:788-790).

This defeats the ECA-71/82 identity guard's own stated threat model (store.py:718-727): a second process holding the same credential racing to claim an identity — exactly the actor who can call `who()`. The codebase already treats `*_token` fields as sensitive (logging_config.py:39 redacts them in logs) but currently publishes one over the wire via `who()`. Mitigated today by loopback-bind + SSH tunnel, but it defeats a control the codebase built specifically to survive a compromised/duplicated credential.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 who() no longer exposes announce_token (or any peer credential) in its response
- [ ] #2 The forget-then-reannounce identity guard still works correctly after the fix
- [ ] #3 A regression test asserts who() output contains no token fields
<!-- AC:END -->
