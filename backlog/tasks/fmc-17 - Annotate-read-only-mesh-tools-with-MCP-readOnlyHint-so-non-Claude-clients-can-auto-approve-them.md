---
id: FMC-17
title: >-
  Annotate read-only mesh tools with MCP readOnlyHint so non-Claude clients can
  auto-approve them
status: Done
assignee:
  - '@claude'
created_date: '2026-08-03 01:03'
updated_date: '2026-08-03 03:10'
labels:
  - codex
  - mcp
dependencies: []
documentation:
  - >-
    backlog/docs/research/doc-3 -
    Codex-CLI-mesh-support-—-feasibility-research-2026-08-02.md
priority: medium
type: enhancement
ordinal: 17000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Our mesh tools declare no MCP tool annotations. Codex CLI (verified on codex-cli 0.145.0) treats an unannotated tool as not read-only, so when a Codex client is configured with `default_tools_approval_mode` of `auto` or `writes`, every mesh call raises an approval request. In headless `codex exec` there is nobody to ask, so the call is auto-denied with `user cancelled MCP tool call` and never reaches the server. Today the only workaround is the blanket `approve` mode, which auto-approves ALL mesh tools including mutating ones such as `approve_tool` and `write_file`.

Annotating the genuinely read-only tools lets any MCP client auto-approve just those and keep prompting for the rest. This is a prerequisite for a least-privilege Codex peer. See doc-3 finding 4.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Every mesh tool is classified read-only or not, based on whether it writes to the store or mutates state (not on whether it merely blocks)
- [x] #2 Read-only tools declare readOnlyHint via FastMCP tool annotations; mutating tools do not
- [x] #3 A headless `codex exec` run configured with default_tools_approval_mode="auto" calls a read-only mesh tool successfully with no approval prompt
- [x] #4 The same run still gets an approval request for a mutating tool such as write_file
- [x] #5 The tool pattern section of CLAUDE.md documents the annotation requirement for new tools
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Classify every @mcp.tool in tools/*.py as read-only or mutating by tracing each into services/store.py and checking for INSERT/UPDATE/DELETE (not by whether the tool blocks). Confirmed read-only (12): wait_for_completion, get_status, list_messages (messaging.py); await_decision, pending_approvals, wait_for_pending_approval (permissions.py); list_files, read_file (files.py); who (presence.py); subscribe (pubsub.py); await_session_op (session_relay.py); await_teams_send (teams_outbox.py). Confirmed mutating (18): send_prompt, interrupt, cancel, wait_for_instruction (claims via pop_next_for_worker's UPDATE to delivered), reply, consume_interrupt (DELETE) (messaging.py); request_approval, approve_tool (permissions.py); write_file (files.py); announce, forget (presence.py); publish (pubsub.py); request_session_op, wait_for_pending_session_ops (claims via list_pending_session_ops' UPDATE pending->claimed), complete_session_op (session_relay.py); request_teams_send, wait_for_pending_teams_send (claims via list_pending_teams_sends' UPDATE pending->claimed), complete_teams_send (teams_outbox.py). Two traps beyond the ones the campaign research called out: wait_for_pending_teams_send and wait_for_pending_session_ops LOOK like their read-only sibling wait_for_pending_approval (same "long-poll for pending X" shape) but actually claim rows (pending->claimed UPDATE) to prevent double-drain (FMC-12 AC#1/#2) -- so they must NOT be annotated read-only despite the naming symmetry.
2. Add `from mcp.types import ToolAnnotations` to each tools/*.py file that gets a read-only annotation, and pass `annotations=ToolAnnotations(readOnlyHint=True)` to the @mcp.tool(...) decorator for each of the 12 read-only tools above. Leave the 18 mutating tools unannotated.
3. Verify AC#3/#4 with live `codex exec` runs against the local mesh server (127.0.0.1:5473, MCP_API_KEY from .env exported not printed): default_tools_approval_mode="auto" must let a read-only tool (e.g. who or list_messages) succeed with no approval prompt, and must still raise an approval request / auto-deny for write_file. Redirect output to a file, use model_reasoning_effort=low to keep cost down.
4. Document the readOnlyHint annotation requirement in CLAUDE.md's "Tool pattern" section (AC#5): new read-only tools must declare it, mutating tools must not.
5. Update backlog: advance tracker cursor to FMC-18, move FMC-17 to Resolved, append session log -- on the branch, before the PR.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Classified all 30 mesh tools via services/store.py (INSERT/UPDATE/DELETE vs pure SELECT), not by blocking behavior. 12 read-only: wait_for_completion, get_status, list_messages, await_decision, pending_approvals, wait_for_pending_approval, list_files, read_file, who, subscribe, await_session_op, await_teams_send. 18 mutating (unannotated): send_prompt, interrupt, cancel, wait_for_instruction, reply, consume_interrupt, request_approval, approve_tool, write_file, announce, forget, publish, request_session_op, wait_for_pending_session_ops, complete_session_op, request_teams_send, wait_for_pending_teams_send, complete_teams_send. Found (beyond the campaign research's own traps) that wait_for_pending_teams_send and wait_for_pending_session_ops LOOK read-only like their sibling wait_for_pending_approval but actually atomically claim rows (pending->claimed UPDATE, per FMC-12) -- left unannotated. Declared annotations=ToolAnnotations(readOnlyHint=True) (mcp.types) on the 12 read-only tools; server boot-check via mcp.list_tools() confirmed exactly 12/18 split. Documented the requirement + both traps in CLAUDE.md's new 'Tool annotations: readOnlyHint' subsection under Tool pattern.

Verified AC#3/#4 with real codex exec runs (codex-cli 0.145.0) against the live mesh server (pm2-restarted to load this branch's code): default_tools_approval_mode="auto" let 'who' (read-only, annotated) succeed with no approval prompt (full JSON roster returned); the same config still auto-denied 'write_file' (mutating, unannotated) with 'user cancelled MCP tool call' -- the exact still-prompts signal doc-3 documented -- and confirmed no file was actually created. Full suite: uv run pytest -> 421 passed, 0 failed. uv run ruff check src/ tests/ -> clean. uv run ruff format --check on touched files -> clean except teams_outbox.py's pre-existing complete_teams_send drift, confirmed via git stash to exist on dev before this branch (same class already documented in FMC-4/6/8/14), untouched by this change.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Classified all 30 mesh tools by store-mutation (not blocking behavior); annotated the 12 genuinely read-only tools with FastMCP's ToolAnnotations(readOnlyHint=True), leaving the 18 mutating tools (including two claim-based traps disguised as read-only siblings) unannotated. Documented the requirement in CLAUDE.md. Verified AC#3/#4 with live codex exec runs against the pm2-restarted mesh server: a read-only tool succeeds under default_tools_approval_mode="auto" with zero prompts, write_file still auto-denies. Full test suite (421 passed) and ruff check both clean.
<!-- SECTION:FINAL_SUMMARY:END -->
