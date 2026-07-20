---
id: FMC-6
title: Fix tool-pattern deviations from CLAUDE.md's documented contract
status: To Do
assignee: []
created_date: '2026-07-20 20:25'
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
- [ ] #1 pending_approvals, get_status, and who each catch ValidationError separately per the documented tool pattern
- [ ] #2 wait_for_pending_approval no longer reaches into Notifier internal state directly
- [ ] #3 Each tool timeout description accurately states its actual enforced cap
<!-- AC:END -->
