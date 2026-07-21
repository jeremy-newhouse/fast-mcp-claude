---
id: FMC-6
title: Fix tool-pattern deviations from CLAUDE.md's documented contract
status: Done
assignee:
  - '@claude'
created_date: '2026-07-20 20:25'
updated_date: '2026-07-21 13:31'
labels:
  - tech-debt
  - tools
dependencies: []
priority: low
type: bug
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by an ad-hoc agent-team dogfooding review (2026-07-20) against CLAUDE.md's documented tool pattern (validate inputs first, always return {"success": bool, ...}, always catch ValidationError separately from Exception).

1. pending_approvals (tools/permissions.py:110), get_status (tools/messaging.py:149), and who (tools/presence.py:128) each have only a bare except Exception, with no separate ValidationError branch, so a validation failure in any of them degrades to a generic UNKNOWN_ERROR response without the field key the documented contract promises callers.

2. wait_for_pending_approval (tools/permissions.py:129-138) reaches directly into store._notifier private state, alongside a dead "from ..services.store import Notifier" import that the code's own comment labels "re-import for clarity". It should be a Store method instead, mirroring the existing wait_for_pending_teams_sends pattern.

3. Every long-poll tool's timeout Field description says the value is "capped by server's poll_max_wait_s", but the actual caps are hardcoded per tool (300.0 in most places; 600.0 at permissions.py:82) and poll_max_wait_s is only ever used as the default. Since tool descriptions are what the calling model reads to decide what timeout to request, this actively misleads it into requesting timeouts that exceed the ~30s MCP idle timeout CLAUDE.md's Known Limitations section warns about.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 pending_approvals, get_status, and who each catch ValidationError separately per the documented tool pattern
- [x] #2 wait_for_pending_approval no longer reaches into Notifier internal state directly
- [x] #3 Each tool timeout description accurately states its actual enforced cap
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Sub-issue 1 (bare except Exception, no ValidationError branch): fix pending_approvals
   (permissions.py) and who (presence.py) with real input validation for their one
   parameter each (limit / stale_seconds), since both can genuinely raise a raw
   ValueError/TypeError today on a malformed value that the bare except swallows into
   a generic UNKNOWN_ERROR. get_status (messaging.py) takes zero parameters and has no
   validate_* call, so it cannot raise ValidationError under any input -- verified by
   reading its body; leaving its except clause unchanged rather than adding a dead/
   unreachable except branch for a scenario that cannot happen.
2. Sub-issue 2: add Store.wait_for_pending_approvals(timeout, limit=50) mirroring the
   existing wait_for_pending_teams_sends pattern; rewire wait_for_pending_approval to
   call it instead of reaching into store._notifier directly; delete the dead Notifier
   re-import and the now-unused _approvals_or_none helper.
3. Sub-issue 3: grep every Field(description=...) paired with a validate_timeout(...,
   cap=X) call across tools/*.py (9 total) and rewrite each to state its own actual cap
   (600s for await_decision, 300s for the other 8) instead of the generic/misleading
   "capped by poll_max_wait_s" phrasing.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Verified before fixing: get_status (messaging.py) takes zero parameters and calls no
validate_* helper, so it structurally cannot raise ValidationError today -- confirmed by
reading its body (only calls store.list_messages/list_pending_approvals with no
user-controlled args). Left its except clause unchanged (bare except Exception only)
rather than add a dead/unreachable except ValidationError branch, per the "don't add
error handling for scenarios that can't happen" principle -- this is the same
verify-first judgment call FMC-8 established for this campaign. AC#1 is satisfied for
the 2 of 3 named tools that had a real gap (pending_approvals, who); get_status's
listing in the task description does not correspond to a live bug.

Confirmed live bugs via git stash (isolated to permissions.py/store.py/presence.py,
tests kept): pending_approvals(limit="not-a-number") raised a bare ValueError from
int(limit) -> UNKNOWN_ERROR/no field; who(stale_seconds="not-a-number") raised a bare
TypeError from `stale_seconds <= 0` -> UNKNOWN_ERROR/no field. Both now return
VALIDATION_ERROR with field set. Added 6 tests (test_permissions.py: 4 new,
test_presence.py: 2 new); all 6 independently confirmed to fail against the pre-fix
code via the same git stash.

Sub-issue 2: added Store.wait_for_pending_approvals(timeout, limit=50) to
services/store.py (mirrors wait_for_pending_teams_sends exactly), rewired
wait_for_pending_approval in permissions.py to call it, removed the dead
`from ..services.store import Notifier` re-import and the now-unused
_approvals_or_none() module function. Regression-tested functionally (creates an
approval, confirms wait_for_pending_approval surfaces it; confirms empty timeout path
too) since there's no "before" behavior change to diff -- same external contract, only
the internal Notifier access path changed.

Sub-issue 3: grepped every validate_timeout(..., cap=X) call site (9 total across
messaging.py x2, permissions.py x2, pubsub.py x1, teams_outbox.py x2, session_relay.py
x2) and rewrote each paired Field(description=...) to state its real cap (600s for
await_decision, 300s for the other 8) instead of the generic "capped by poll_max_wait_s"
wording, which only reflects the default, not the enforced ceiling.

Verified: uv run pytest (290 passed, up from 284); uv run ruff check src/ tests/
(clean). uv run ruff format --check flags services/store.py and tools/teams_outbox.py,
but confirmed via git stash this drift pre-dates this branch (present on dev @ 141b7ed
before any FMC-6 edits) -- same pre-existing drift FMC-4 already documented, not
introduced by this branch and not touching any line this branch edited.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed all 3 tool-pattern deviations from CLAUDE.md's documented contract.

(1) pending_approvals (permissions.py) and who (presence.py) now validate their one
input parameter (limit / stale_seconds) and catch ValidationError separately, so a
malformed value returns VALIDATION_ERROR with a field instead of a generic
UNKNOWN_ERROR -- confirmed both were live bugs (raw ValueError/TypeError) via git
stash before fixing. get_status (messaging.py) takes no parameters and cannot raise
ValidationError under any input, verified by reading its body; left unchanged rather
than add a dead except branch for a scenario that cannot happen.

(2) wait_for_pending_approval no longer reaches into store._notifier: added
Store.wait_for_pending_approvals(timeout, limit=50) mirroring the existing
wait_for_pending_teams_sends pattern, removed the dead Notifier re-import and the
now-unused _approvals_or_none() helper.

(3) All 9 long-poll tools' timeout Field descriptions now state their actual enforced
cap (600s for await_decision, 300s for the other 8) instead of the misleading generic
"capped by poll_max_wait_s" wording.

Added 6 tests (290 total, up from 284); the 2 regression tests for sub-issue 1 each
confirmed to fail against the pre-fix code via git stash. Verified: uv run pytest (290
passed), uv run ruff check src/ tests/ (clean). ruff format flags 2 files
(store.py/teams_outbox.py) with pre-existing drift confirmed present on dev before this
branch (same class of drift FMC-4 already documented), not introduced here.
<!-- SECTION:FINAL_SUMMARY:END -->
