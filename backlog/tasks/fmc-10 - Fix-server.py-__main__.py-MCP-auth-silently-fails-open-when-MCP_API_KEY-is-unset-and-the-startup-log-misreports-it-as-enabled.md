---
id: FMC-10
title: >-
  Fix server.py/__main__.py: MCP auth silently fails open when MCP_API_KEY is
  unset, and the startup log misreports it as enabled
status: Done
assignee:
  - '@jeremy-newhouse'
created_date: '2026-07-21 14:44'
updated_date: '2026-07-21 17:16'
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
- [x] #1 When authentication is enabled (mcp_auth_enabled is true) but no usable API key is configured (mcp_api_key is None or an empty string), the server refuses to start (fails closed with a clear startup error) instead of starting up and serving MCP tools with auth=None
- [x] #2 The startup configuration log's auth_enabled field accurately reflects whether requests will actually be authenticated at runtime - it must not report true for an empty-string MCP_API_KEY (or any other case where the server would in fact run unauthenticated)
- [x] #3 Both the fail-open startup behavior and the misleading startup log field are covered by tests that fail against the current code and pass after the fix
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. config.py: add a Settings.mcp_auth_effective property = bool(mcp_api_key) and mcp_auth_enabled -- single source of truth for whether requests will actually be authenticated at runtime, replacing two independently-drifted boolean expressions.
2. server.py: restructure the auth-selection block (lines ~26-35) to a nested if: when mcp_auth_enabled is True, require a truthy mcp_api_key or raise RuntimeError with a clear message (fail closed) before ApiKeyVerifier/FastMCP construction; when mcp_auth_enabled is False, keep the existing explicit-opt-out warning unchanged.
3. __main__.py: replace the auth_enabled log field's 'mcp_api_key is not None and mcp_auth_enabled' expression with settings.mcp_auth_effective.
4. tests/test_server.py (new): reload fast_mcp_claude.server with monkeypatched env (auth enabled + empty/None key) and assert RuntimeError is raised (fails pre-fix); assert normal construction succeeds when key is set or auth is explicitly disabled.
5. tests in the same file (or test_config.py) for Settings.mcp_auth_effective covering the empty-string-key case explicitly (fails pre-fix against the old __main__ expression semantics, passes after).
6. Run uv run pytest and uv run ruff check/format; record AC evidence on the task.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Deviated from step 4 of the recorded plan: instead of reloading fast_mcp_claude.server under monkeypatched env vars (risks leaving sys.modules in a broken state on a raise mid-reload), extracted the auth decision into a plain function server.build_auth_provider(settings) that takes a Settings instance directly -- trivially unit-testable with settings_factory, no reload/import gymnastics. Added Settings.mcp_auth_effective (config.py) as the single source of truth both build_auth_provider() and __main__.py's startup log now read, so the two can't independently drift again the way 'mcp_api_key and mcp_auth_enabled' vs 'mcp_api_key is not None and mcp_auth_enabled' did.

Verification: added tests/test_server.py (8 tests). Confirmed they fail against the pre-fix code -- git stash of src/fast_mcp_claude/{server,config,__main__}.py (keeping the new test file) produces an ImportError at collection (build_auth_provider/mcp_auth_effective don't exist yet), not a pass. Restored the fix and reran: 'uv run pytest tests/test_server.py -v' -> 8 passed. Full suite: 'uv run pytest' -> 298 passed (up from 290 pre-branch). 'uv run ruff check src/ tests/' -> All checks passed. 'uv run ruff format --check' on the 4 touched files -> already formatted.

Post-implementation review (adversarial subagent pass, general-purpose agent): no blocking findings. Two minor findings addressed: (1) build_auth_provider re-derived the auth-effective logic by hand instead of delegating to the new mcp_auth_effective property it claimed was the single source of truth -- reordered to check mcp_auth_enabled first, then raise off settings.mcp_auth_effective directly, so there's exactly one place that computes this. (2) .env.example's auth comment was now stale (implied unset-key-with-default-auth-enabled was a supported 'unauthenticated on 127.0.0.1' mode) -- updated to state the server now refuses to start in that config. Checked and left out of scope: launcher.py:1453's cfg.mcp_auth_enabled/cfg.mcp_api_key_present check is a separate LauncherConfig snapshot for a different CLI process (fast-mcp-claude-launcher checking a peer's remote auth posture before spawning), not Settings itself -- not a drift risk this task should touch. Also noted as a candidate follow-up, not fixed here (not part of the 2 described bugs): a whitespace-only MCP_API_KEY (e.g. " ") is still treated as a valid key by both mcp_auth_effective and build_auth_provider since bool(" ") is True. Re-verified after these changes: 'uv run pytest' 298 passed, 'uv run ruff check'/'format --check' clean.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed both bugs in the auth bootstrap path. (1) server.py now fails closed: extracted the auth-provider decision into build_auth_provider(settings), which raises RuntimeError with a clear message when MCP_AUTH_ENABLED=true but MCP_API_KEY is None/empty, instead of silently constructing FastMCP with auth=None and serving every tool unauthenticated. (2) __main__.py's startup log auth_enabled field now reads settings.mcp_auth_effective (new config.py property: bool(mcp_api_key) and mcp_auth_enabled) instead of the buggy 'mcp_api_key is not None and mcp_auth_enabled', which misreported true for an empty-string key -- the property is a single source of truth so server.py's startup check and the log can't drift apart again. Added tests/test_server.py (8 tests) covering both the fail-closed raise (None key, empty-string key, key-set success, auth-disabled no-op) and mcp_auth_effective's 4 truth-table cases. Verified via git stash that all 8 fail against pre-fix code (ImportError, the helpers didn't exist), pass after the fix; full suite 298 passed (up from 290); ruff check and format clean. All 3 ACs checked.
<!-- SECTION:FINAL_SUMMARY:END -->
