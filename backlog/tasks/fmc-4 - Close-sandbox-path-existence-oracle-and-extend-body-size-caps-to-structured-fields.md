---
id: FMC-4
title: >-
  Close sandbox path-existence oracle and extend body-size caps to structured
  fields
status: To Do
assignee: []
created_date: '2026-07-20 20:25'
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
- [ ] #1 read_file and write_file reject out-of-sandbox paths before checking whether they exist, so responses no longer reveal existence of paths outside WORKSPACE_ROOTS
- [ ] #2 All structured fields listed in the task description (metadata, tool_input, payload, result) enforce an explicit size cap
- [ ] #3 Both fixes are covered by tests
<!-- AC:END -->
