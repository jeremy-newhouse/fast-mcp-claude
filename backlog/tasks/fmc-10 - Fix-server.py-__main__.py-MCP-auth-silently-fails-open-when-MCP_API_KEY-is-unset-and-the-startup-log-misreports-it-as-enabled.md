---
id: FMC-10
title: >-
  Fix server.py/__main__.py: MCP auth silently fails open when MCP_API_KEY is
  unset, and the startup log misreports it as enabled
status: To Do
assignee: []
created_date: '2026-07-21 14:44'
labels:
  - security
  - auth
dependencies: []
references:
  - backlog/docs/reviews/doc-2 - Codex-full-codebase-review-2026-07-21.md
priority: high
type: bug
ordinal: 10000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by a second-opinion review (OpenAI Codex, gpt-5.6-sol, ultra effort) of the full codebase on 2026-07-21, independently re-verified against the current code in this same session (file and line traced, behavior confirmed by reading the actual branches).

Two distinct bugs in the same authentication bootstrap path, both in the "auth is on but misconfigured" case:

1. server.py silently serves an unauthenticated endpoint (server.py, around lines 26-35). mcp_auth_enabled defaults to true (config.py, documented default). The auth-selection block is:
   if settings.mcp_api_key and settings.mcp_auth_enabled: build the ApiKeyVerifier
   elif not settings.mcp_auth_enabled: warn "MCP_AUTH_ENABLED=false" (explicit opt-out, fine)
   else: warn "MCP_API_KEY not set" and do nothing else
   In that final else branch, mcp_auth_enabled is true but mcp_api_key is None or an empty string, so auth_provider stays None. The FastMCP instance is then constructed a few lines later with auth=auth_provider, i.e. auth=None. There is no exception, no sys.exit, no refusal to start - the process comes up normally and FastMCP serves every registered tool (messaging, permissions, files, pubsub, presence - everything in the tool inventory) to any caller with network access, with zero bearer-token check. This is the exact misconfiguration an operator is most likely to hit (forgetting to set MCP_API_KEY in .env, or setting it to an empty string via a blank environment variable) and it fails open instead of fail closed, silently turning what is supposed to be a mutual-bearer-authenticated peer mesh into an open endpoint. This is a distinct bug from the already-closed FMC-3 (auth.py's ApiKeyVerifier.verify_token had a process-global lockout bug and a non-ASCII bearer-token crash) - FMC-3 is about the token verifier's internal logic once it exists; this bug is about server.py's decision of whether to construct a verifier at all, one layer up, and applies even though FMC-3 is already fixed.

2. __main__.py's startup log line can claim auth is enabled when it is not (__main__.py, line 27). The "Server configuration" log entry computes its auth_enabled field as:
   settings.mcp_api_key is not None and settings.mcp_auth_enabled
   This check only asks whether mcp_api_key is not None - it does not check for an empty string. If MCP_API_KEY is set to an empty string in the environment (empty but technically not None) and mcp_auth_enabled is true, this expression evaluates to true and the log prints auth_enabled: true at startup. But per bug 1 above, the actual runtime behavior in that exact configuration is a fully unauthenticated server (auth_provider is None, auth=None). So the one log line an operator would check to confirm the server came up secured actively confirms the opposite of what is true, with no other signal in the startup log to catch the misconfiguration. This makes bug 1 strictly harder to notice in production.

Together these mean: a server can start, log that authentication is enabled, and serve every MCP tool over the network to unauthenticated callers, with no indication anywhere in the process output that anything is wrong.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 When authentication is enabled (mcp_auth_enabled is true) but no usable API key is configured (mcp_api_key is None or an empty string), the server refuses to start (fails closed with a clear startup error) instead of starting up and serving MCP tools with auth=None
- [ ] #2 The startup configuration log's auth_enabled field accurately reflects whether requests will actually be authenticated at runtime - it must not report true for an empty-string MCP_API_KEY (or any other case where the server would in fact run unauthenticated)
- [ ] #3 Both the fail-open startup behavior and the misleading startup log field are covered by tests that fail against the current code and pass after the fix
<!-- AC:END -->
