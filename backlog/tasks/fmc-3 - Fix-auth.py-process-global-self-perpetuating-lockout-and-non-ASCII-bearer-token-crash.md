---
id: FMC-3
title: >-
  Fix auth.py: process-global self-perpetuating lockout and non-ASCII
  bearer-token crash
status: Done
assignee:
  - '@claude'
created_date: '2026-07-20 20:25'
updated_date: '2026-07-20 21:51'
labels:
  - security
  - auth
dependencies: []
priority: high
type: bug
ordinal: 3000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by an ad-hoc agent-team dogfooding review (2026-07-20) of auth.py against CLAUDE.md's documented security model.

Two bugs in the same file:

1. Process-global self-perpetuating lockout (auth.py:37-59). AuthRateLimiter keeps one failure counter for the entire server, not per-peer/IP. While locked out, verify_token returns None for every caller, including the legitimate peer. record_success is the only thing that clears _failed_attempts, but it is unreachable while locked out. Failure entries live 300s while the lockout window is only 60s, so a single additional bad request roughly once a minute keeps the whole mesh endpoint down indefinitely with no credential required.

2. hmac.compare_digest raises on non-ASCII tokens (auth.py:79). compare_digest with str arguments requires ASCII; a bearer containing any non-ASCII character raises TypeError out of verify_token, producing an unhandled 500 instead of a clean 401. record_failure() is skipped on that path, so those attempts never count toward the rate limiter either.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A locked-out attacker can no longer indefinitely block the legitimate peer from authenticating
- [x] #2 A bearer token with non-ASCII characters is rejected with a normal 401 response, not an unhandled 500
- [x] #3 Both scenarios are covered by tests
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Fix bug 1 (process-global self-perpetuating lockout) in ApiKeyVerifier.verify_token:
   reorder so the token comparison happens FIRST, before consulting the rate limiter.
   A request bearing the correct api_key always succeeds and calls record_success(),
   regardless of any active lockout. The rate limiter (check_rate_limit + record_failure)
   only gates the path where the token does NOT match. This is necessary because
   fastmcp's TokenVerifier.verify_token(token: str) receives no per-connection identity
   (verified: no client IP/peer name available at this layer) so there is no way to
   scope the limiter per-source; the only viable fix is to never let the shared lockout
   block a holder of the correct credential.
2. Fix bug 2 (non-ASCII bearer crash) by encoding both sides to UTF-8 bytes before
   hmac.compare_digest, e.g. compare_digest(token.encode(), self.api_key.encode()).
   bytes-vs-bytes comparison never raises on non-ASCII content (str-vs-str does), so
   a non-ASCII bearer now falls through to the normal wrong-token path -> None (401),
   and correctly counts toward record_failure() (previously skipped because the
   TypeError propagated out of verify_token before record_failure was reached).
3. Add tests to tests/test_auth.py (existing TestApiKeyVerifier / TestAuthRateLimiter
   classes, matching their fixture/style):
   - valid token succeeds even while the rate limiter is in an active lockout
     (drive 5 wrong guesses to trigger lockout, then assert the correct key still
     returns an AccessToken)
   - a non-ASCII bearer token returns None without raising, and counts as a recorded
     failure (check it contributes toward triggering lockout like any other failure)
4. Run uv run pytest tests/test_auth.py -v, full uv run pytest, and
   uv run ruff check src/ tests/ before finalizing.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Fixed both bugs in src/fast_mcp_claude/auth.py::ApiKeyVerifier.verify_token: (1) reordered so the token comparison runs before consulting AuthRateLimiter — a correct api_key now always succeeds and calls record_success(), even during an active lockout, since verify_token receives no per-connection identity (confirmed: fastmcp.server.auth.TokenVerifier.verify_token(token: str) only) so the limiter can't be scoped to just the attacker; (2) encoded both sides to UTF-8 bytes before hmac.compare_digest so a non-ASCII bearer falls through to the normal wrong-token path (None/401) instead of raising TypeError, and now correctly counts toward record_failure(). Added 3 tests to tests/test_auth.py: test_non_ascii_key_returns_none_not_raises, test_non_ascii_failure_counts_toward_lockout, test_valid_key_succeeds_during_active_lockout. Verified: uv run pytest tests/test_auth.py -v -> 10 passed; uv run pytest (full suite) -> 251 passed; uv run ruff check src/ tests/ -> All checks passed.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed the process-global self-perpetuating lockout (a correct bearer token now always authenticates even during an active lockout, since the rate limiter has no per-source identity to scope itself to just the attacker) and the non-ASCII bearer crash (hmac.compare_digest now runs on UTF-8-encoded bytes, so a non-ASCII token yields a clean None/401 and is correctly counted as a failure) in src/fast_mcp_claude/auth.py::ApiKeyVerifier.verify_token. Verified with 3 new tests in tests/test_auth.py covering both scenarios plus the existing suite: uv run pytest tests/test_auth.py -v (10 passed), uv run pytest (251 passed), uv run ruff check src/ tests/ (clean).
<!-- SECTION:FINAL_SUMMARY:END -->
