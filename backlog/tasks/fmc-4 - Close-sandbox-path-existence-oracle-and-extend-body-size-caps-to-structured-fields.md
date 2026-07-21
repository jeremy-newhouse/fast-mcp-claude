---
id: FMC-4
title: >-
  Close sandbox path-existence oracle and extend body-size caps to structured
  fields
status: Done
assignee:
  - '@claude'
created_date: '2026-07-20 20:25'
updated_date: '2026-07-21 08:43'
labels:
  - security
  - sandbox
dependencies: []
priority: medium
type: bug
ordinal: 4000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by an ad-hoc agent-team dogfooding review (2026-07-20).

1. Sandbox existence oracle (utils/validation.py:160-161). The must_exist check runs before the workspace-root containment check, so read_file("/etc/shadow") and read_file("/etc/nope") return distinguishable errors, and the error message echoes the resolved path. An authenticated peer can probe for the existence of arbitrary absolute paths outside WORKSPACE_ROOTS by reading the error type. Swapping the order of the two checks fixes it. (The symlink-escape logic itself is sound — resolve(strict=False) against pre-resolved roots in config.py:232 correctly catches both final-component and dangling symlinks; this bug is purely about check ordering.)

2. Body-size caps do not cover several structured fields (CLAUDE.md's documented caps: prompt <=1MB, response <=4MB, file <=10MB, pubsub payload <=256KB). The following fields are json.dumps'd straight into SQLite with no size enforcement at all: send_prompt's metadata (tools/messaging.py:69-74), request_approval's tool_input/tool_name (tools/permissions.py:50), announce's metadata (tools/presence.py:61), request_session_op's payload (tools/session_relay.py:69), complete_session_op's result (tools/session_relay.py:125), request_teams_send's metadata (tools/teams_outbox.py:72). An authenticated peer can bypass every documented cap by routing an oversized payload through any of these fields.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 read_file and write_file reject out-of-sandbox paths before checking whether they exist, so responses no longer reveal existence of paths outside WORKSPACE_ROOTS
- [x] #2 All structured fields listed in the task description (metadata, tool_input, payload, result) enforce an explicit size cap
- [x] #3 Both fixes are covered by tests
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. utils/validation.py: reorder validate_workspace_path so the WORKSPACE_ROOTS
   containment check runs BEFORE the must_exist check, so an out-of-sandbox path
   always raises PermissionDeniedError (never a distinguishable ValidationError
   based on whether the path exists). Leaves the symlink-escape resolve() logic
   untouched.
2. utils/validation.py: add a generic validate_json_object_size(value, max_bytes,
   field) helper (json import moved to module top); refactor the existing
   validate_pubsub_payload to delegate to it (same external behavior). Add
   MAX_METADATA_BYTES=256_000 (precedent: existing pubsub cap, for the 4
   metadata/payload/result-shaped dict fields), MAX_TOOL_INPUT_BYTES=1_000_000
   (precedent: prompt-scale, since tool_input can carry e.g. a large file write),
   MAX_TOOL_NAME_BYTES=256. Add validate_metadata() (optional dict, used by
   metadata/payload/result fields), validate_tool_name(), validate_tool_input().
3. Wire the new validators into the 6 flagged call sites: messaging.send_prompt
   (metadata), permissions.request_approval (tool_name, tool_input),
   presence.announce (metadata), session_relay.request_session_op (payload) +
   complete_session_op (result), teams_outbox.request_teams_send (metadata).
4. Tests: tests/test_validation.py gets an existence-oracle-ordering regression
   test (same error type regardless of existence) plus unit tests for the new
   validators; add/extend tool-level wiring tests (new test_messaging.py and
   test_permissions.py following the existing wired_who fixture pattern in
   test_presence.py, plus new cases in test_presence.py/test_session_relay.py/
   test_teams_outbox.py) confirming an oversized field is rejected with
   VALIDATION_ERROR at each of the 6 sites.
5. Verify: uv run pytest (full suite) + uv run ruff check src/ tests/.
6. Independent adversarial-review subagent on the branch diff before opening the
   PR (security-labeled task, higher risk than FMC-8).
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Fix 1 (existence oracle): reordered validate_workspace_path (utils/validation.py)
so the WORKSPACE_ROOTS containment check runs before must_exist. Empirically
confirmed the pre-fix bug by exec'ing the old function body standalone against a
tmp dir: an existing out-of-sandbox path raised PermissionDeniedError while a
missing one raised ValidationError -- two distinguishable exception types/messages,
confirming the oracle. Post-fix both raise PermissionDeniedError uniformly
(tests/test_validation.py::test_out_of_sandbox_rejection_does_not_leak_existence).
Left config.py:232's symlink-resolve logic untouched per the task's own note.

Fix 2 (size caps): added validate_json_object_size() generic helper +
MAX_METADATA_BYTES=256_000 (pubsub precedent), MAX_TOOL_INPUT_BYTES=1_000_000
(prompt-scale precedent, since tool_input can carry e.g. a large file write),
MAX_TOOL_NAME_BYTES=256. Wired into all 6 flagged fields: send_prompt.metadata,
request_approval.tool_name/tool_input, announce.metadata,
request_session_op.payload, complete_session_op.result,
request_teams_send.metadata. validate_pubsub_payload refactored to delegate to
the new generic helper (unchanged external behavior).

Tests added: 14 unit tests in test_validation.py (oracle-ordering + new
validators) + 8 tool-layer wiring tests across test_messaging.py (new),
test_permissions.py (new), test_presence.py, test_session_relay.py,
test_teams_outbox.py, one per flagged field, confirming VALIDATION_ERROR on an
oversized/oversized-name payload via the actual @mcp.tool entrypoint (not just
the validator in isolation).

Verified: uv run pytest (277 passed, up from 257); uv run ruff check src/ tests/
(clean). uv run ruff format --check flags 9 files including teams_outbox.py, but
confirmed via git stash that this formatting drift pre-existed on dev before this
branch -- not introduced by this change.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed 2 sandbox/security bugs. (1) Existence oracle: swapped check order in validate_workspace_path (utils/validation.py) so WORKSPACE_ROOTS containment is checked before must_exist, so an out-of-sandbox path always raises PermissionDeniedError regardless of whether it exists -- empirically confirmed the pre-fix code raised different exception types (PermissionDeniedError vs ValidationError) for existing vs missing out-of-sandbox paths by exec'ing the old function standalone. (2) Body-size caps: added a generic validate_json_object_size() helper plus MAX_METADATA_BYTES/MAX_TOOL_INPUT_BYTES/MAX_TOOL_NAME_BYTES caps, wired into all 6 flagged fields (send_prompt.metadata, request_approval.tool_name/tool_input, announce.metadata, request_session_op.payload, complete_session_op.result, request_teams_send.metadata). Added 14 validator unit tests + 8 tool-layer wiring tests (2 new test files: test_messaging.py, test_permissions.py). Verified: uv run pytest (277 passed, was 257), uv run ruff check src/ tests/ (clean). All 3 ACs checked.
<!-- SECTION:FINAL_SUMMARY:END -->
