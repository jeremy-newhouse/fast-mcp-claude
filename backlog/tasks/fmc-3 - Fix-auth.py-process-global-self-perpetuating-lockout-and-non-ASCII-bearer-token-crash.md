---
id: FMC-3
title: >-
  Fix auth.py: process-global self-perpetuating lockout and non-ASCII
  bearer-token crash
status: To Do
assignee: []
created_date: '2026-07-20 20:25'
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
- [ ] #1 A locked-out attacker can no longer indefinitely block the legitimate peer from authenticating
- [ ] #2 A bearer token with non-ASCII characters is rejected with a normal 401 response, not an unhandled 500
- [ ] #3 Both scenarios are covered by tests
<!-- AC:END -->
