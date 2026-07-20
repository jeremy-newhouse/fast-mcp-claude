---
id: FMC-2
title: 'Fix: who() leaks announce_token, defeating the ECA-71 identity guard'
status: Done
assignee:
  - '@claude'
created_date: '2026-07-20 20:25'
updated_date: '2026-07-20 22:07'
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
- [x] #1 who() no longer exposes announce_token (or any peer credential) in its response
- [x] #2 The forget-then-reannounce identity guard still works correctly after the fix
- [x] #3 A regression test asserts who() output contains no token fields
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Redact at the who() TOOL BOUNDARY in tools/presence.py, not in the shared
   store.list_presence()/_row_to_presence() row-mapper. Verified _row_to_presence
   has exactly one caller (list_presence), and list_presence has exactly one
   caller (who()) -- so a store-layer redaction is production-safe -- BUT
   tests/test_presence.py already asserts store.list_presence() returns the raw
   announce_token (test_announce_refuses_second_live_process et al, used to
   verify the ECA-71 guard end-to-end). Redacting in the store would break that
   established test contract for no security benefit (store.list_presence() is
   an internal Store API, not network-exposed). Redact only in presence.py's
   who() tool, which is the actual externally-callable surface.
2. Add a small helper in tools/presence.py that strips any metadata key matching
   the existing sensitive-field rules already defined in logging_config.py
   (SENSITIVE_LOG_FIELDS_EXACT / SENSITIVE_LOG_SUFFIXES) -- reuse, don't
   duplicate the blacklist. Strip the key entirely (not mask-to-"[REDACTED]"),
   since AC #3 wants "no token fields" in the output.
3. Apply that helper to each peer's metadata in who() before returning.
4. forget_presence() and announce()'s owner-token guard both read the token via
   their own raw SQL queries (not via _row_to_presence), so AC #2 (guard keeps
   working) needs no code change -- confirm via the existing guard test suite
   passing unchanged.
5. Add a regression test asserting who()'s returned peer metadata contains no
   *_token key (AC #3), following test_presence.py's existing store-fixture
   style, calling the who() tool function directly.
6. Run uv run pytest and uv run ruff check; check all 3 ACs with that evidence.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implemented: redact at the who() tool boundary in tools/presence.py (not in
store.list_presence()/_row_to_presence()), since store.list_presence() has no
other caller besides who() but IS asserted directly by existing tests
(test_announce_refuses_second_live_process etc.) to still expose the raw
announce_token for verifying the ECA-71 guard -- redacting at the store layer
would have broken that established contract for no security benefit, since
store.list_presence() is not itself network-exposed.

Added _redact_peer_metadata() in tools/presence.py, reusing the existing
SENSITIVE_LOG_FIELDS_EXACT/SENSITIVE_LOG_SUFFIXES blacklist from
logging_config.py rather than duplicating it. who() now strips any matching
key (announce_token, and any other *_token/*_secret/*_password/*_credential
key) from each peer's metadata dict entirely before returning -- not masked
to a placeholder, since AC #3 asks for "no token fields".

Confirmed forget_presence() and announce()'s owner-token guard both read
metadata.announce_token via their own raw SQL queries in store.py, never via
_row_to_presence -- so AC #2 needed no code change; verified by the full
pre-existing guard test suite (test_announce_refuses_second_live_process,
test_announce_same_token_reannounces, test_announce_tokenless_never_refused,
test_announce_stale_token_reclaimed, test_forget_presence_token_*) passing
unmodified, plus a new end-to-end test exercising forget-then-reannounce
through who().

Added 2 tests to tests/test_presence.py: test_who_redacts_announce_token
(AC #1 + #3) and test_who_redact_guard_still_lets_reannounce_work (AC #2).
Both need who() to read the test's isolated store/settings rather than the
real fast_mcp_claude.server globals (who() closes over module-level names
bound at import time) -- added a `wired_who` fixture that monkeypatches
fast_mcp_claude.tools.presence.store/settings to the test's Store instance.

Verified: `uv run pytest tests/test_presence.py -v` (15 passed, all
pre-existing tests passing unchanged); full `uv run pytest` (253 passed);
`uv run ruff check src/ tests/` (clean).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed the announce_token leak in who(): src/fast_mcp_claude/tools/presence.py
now redacts credential-shaped metadata keys (announce_token and anything
matching logging_config.py's existing SENSITIVE_LOG_FIELDS_EXACT/
SENSITIVE_LOG_SUFFIXES blacklist) from each peer's metadata before returning
from who(), at the tool boundary rather than in the shared store layer (which
existing tests rely on to still expose the raw token internally). The
forget()/announce() owner-token guard is unaffected -- it reads the token via
its own raw SQL query in store.py, not through the redacted path. Verified:
uv run pytest tests/test_presence.py -v (15 passed, including 2 new tests
covering AC #1/#3 and AC #2 end-to-end); full uv run pytest (253 passed);
uv run ruff check src/ tests/ (clean). All 3 acceptance criteria checked.
<!-- SECTION:FINAL_SUMMARY:END -->
