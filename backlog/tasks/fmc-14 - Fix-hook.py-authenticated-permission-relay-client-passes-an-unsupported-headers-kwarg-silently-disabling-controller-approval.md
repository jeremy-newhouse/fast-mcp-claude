---
id: FMC-14
title: >-
  Fix hook.py: authenticated permission-relay client passes an unsupported
  headers kwarg, silently disabling controller approval
status: Done
assignee:
  - '@claude'
created_date: '2026-07-21 14:44'
updated_date: '2026-07-21 17:27'
labels:
  - security
  - hook
dependencies: []
references:
  - backlog/docs/reviews/doc-2 - Codex-full-codebase-review-2026-07-21.md
priority: high
type: bug
ordinal: 14000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by a second-opinion review (OpenAI Codex, gpt-5.6-sol, ultra effort) of the full codebase (2026-07-21), independently re-verified against the actual current code and reproduced live in this session.

Bug: hook.py's authenticated relay path constructs its fastmcp Client with a headers keyword argument (hook.py around lines 153-157):

  client_kwargs: dict[str, Any] = {}
  if api_key:
      client_kwargs["headers"] = {"Authorization": f"Bearer {api_key}"}
  async with Client(url, **client_kwargs) as c:

The project's installed fastmcp dependency is pinned to version 3.4.4 (see pyproject.toml: fastmcp>=3.4.4,<4.0.0, and uv.lock resolving that exact version). That version's actual Client constructor does not accept a headers keyword argument at all -- the real parameter for supplying credentials is named auth, not headers. Passing headers therefore raises a TypeError immediately, before the client ever attempts a connection. This was reproduced live by running the hook directly with an API key configured.

That TypeError is caught by main()'s top-level exception handler (hook.py lines 100-103), which logs it in debug mode and falls back to a permissionDecision of ask via _fallback_ask. So the documented invariant that the hook never silently denies still technically holds -- Claude Code's local permission UI takes over instead of hanging or blocking. But the practical effect is that the entire authenticated-relay code path inside _relay() is unreachable dead code: whenever MCP_API_KEY is configured, which CLAUDE.md's own documented security model (the Mutual bearer auth section) treats as the normal, expected case for any real deployment -- not an edge case -- the hook can never successfully construct a Client, so it can never reach the request_approval or await_decision calls that request_approval and await_decision exist to serve. Every single tool call made by a worker session in an authenticated deployment silently and permanently falls through to Claude Code's own local permission UI instead of ever giving a remote controller the chance to approve or deny it. The controller-approval feature described throughout this project's architecture (see the Permission relay flow section of CLAUDE.md) is completely non-functional today for any authenticated setup -- and there is no error surfaced to the operator beyond an internal debug-only fallback log line (only visible when CRM_HOOK_DEBUG=1), so an operator running an authenticated mesh has no visible signal that remote approval is silently disabled.

Note for whoever picks this up: this is a different hook.py defect than the one referenced in FMC-5's description (that finding was about the elapsed += chunk timeout-accounting loop inside the retry loop of _relay(), around what is now line 192, and FMC-5's own implementation notes say it left hook.py untouched because nothing there needed changing after the Notifier fix). This new bug is about Client construction with an unsupported keyword and is unrelated to that earlier finding -- fixing this one does not touch or reintroduce the FMC-5 territory, and fixing FMC-5 did not touch or fix this one.

This also is not covered by any prior FMC task: FMC-2 through FMC-8 never modified hook.py's Client construction, and this specific TypeError was never previously identified or fixed.

Fix direction (for context, not a mandate): construct the Client using whatever mechanism fastmcp 3.4.4's actual Client constructor supports for bearer-style authentication (its auth parameter, or an httpx.Auth-compatible object, or a transport-level headers option if the transport type accepts one directly) -- verify against the installed fastmcp 3.4.4 API (not assumptions or older fastmcp docs) before choosing an approach, since this exact mismatch is what caused the original bug.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 When MCP_API_KEY is configured, the hook's authenticated relay path successfully constructs a client against the installed fastmcp version without raising a TypeError, and successfully calls request_approval and await_decision against a running local server
- [x] #2 An authenticated deployment (MCP_API_KEY set) can complete a full controller-approval round trip end to end: a worker's tool call reaches the local server's request_approval, a simulated controller decision via approve_tool is observed by await_decision, and the hook emits the corresponding allow or deny permissionDecision instead of falling back to ask
- [x] #3 A regression test exercises the authenticated relay path (Client construction plus at least one call_tool round trip) against the actual installed fastmcp version so a future incompatible client-construction change fails CI instead of silently degrading to the ask fallback
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Verify installed fastmcp 3.4.4 Client constructor API directly (not memory/docs) -- confirmed via inspect.signature: Client has no headers param, but has auth: httpx.Auth | Literal['oauth'] | str | None, and StreamableHttpTransport._set_auth wraps a plain str in BearerAuth automatically.
2. Fix hook.py _relay() to construct Client(url, auth=api_key or None) instead of passing an unsupported headers kwarg.
3. Add tests/test_hook.py: a relay_server fixture that serves the REAL request_approval/await_decision/pending_approvals/approve_tool tool functions (rebound to an isolated Store via monkeypatch, not the process singleton) over a real ephemeral HTTP port guarded by the real ApiKeyVerifier, then two full round-trip tests (allow, deny) driving hook._relay() as the worker side and a second authenticated Client as the controller side calling approve_tool, plus a wrong-api-key rejection test.
4. Confirm regression coverage: git stash hook.py's fix and confirm the round-trip tests fail with the exact TypeError described in the bug (Client.__init__() got an unexpected keyword argument 'headers'), then restore the fix.
5. Run full test suite + ruff check/format.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Verified installed fastmcp 3.4.4 Client.__init__ signature directly (inspect.signature) and StreamableHttpTransport._set_auth source: no headers kwarg exists; auth accepts httpx.Auth | Literal['oauth'] | str | None, and a plain str is auto-wrapped in BearerAuth (Authorization: Bearer <token>). Fixed hook.py's _relay() to use Client(url, auth=api_key or None). Added tests/test_hook.py with a relay_server fixture serving the real permissions.py tool functions (request_approval/await_decision/pending_approvals/approve_tool) rebound to an isolated Store via monkeypatch onto a fresh FastMCP instance guarded by the real ApiKeyVerifier, over a real ephemeral TCP port -- so the test exercises genuine installed-fastmcp Client/server/auth code, not stubs. 3 new tests: allow round trip, deny round trip, wrong-api-key rejection.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed hook.py's _relay(): replaced Client(url, headers={...}) -- an unsupported kwarg that raised TypeError on every authenticated call, per inspect.signature(Client.__init__) against the actual installed fastmcp 3.4.4 -- with Client(url, auth=api_key or None), the constructor's real bearer-token mechanism (a plain str auth value is auto-wrapped in BearerAuth by StreamableHttpTransport._set_auth). Added tests/test_hook.py (3 tests) that serve the real request_approval/await_decision/pending_approvals/approve_tool tools over a real HTTP port guarded by the real ApiKeyVerifier, driving hook._relay() as the worker side and a second authenticated Client as the controller: full allow round trip, full deny round trip, and wrong-api-key rejection. Confirmed via git stash that both round-trip tests fail against the pre-fix code with the exact predicted TypeError (Client.__init__() got an unexpected keyword argument 'headers'), and pass after the fix. Full suite: 301 passed (up from 298); ruff check clean; ruff format --check flags the same 9 pre-existing-drift files already documented in FMC-4/6/8's session notes, none touched by this branch. All 3 ACs verified with objective evidence.
<!-- SECTION:FINAL_SUMMARY:END -->
