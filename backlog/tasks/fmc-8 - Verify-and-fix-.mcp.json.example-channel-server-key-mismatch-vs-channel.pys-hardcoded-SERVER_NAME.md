---
id: FMC-8
title: >-
  Verify and fix .mcp.json.example channel server-key mismatch vs channel.py's
  hardcoded SERVER_NAME
status: To Do
assignee: []
created_date: '2026-07-20 20:26'
labels:
  - channels
  - config
dependencies: []
priority: medium
type: bug
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by an ad-hoc agent-team dogfooding review (2026-07-20) of the repo's documentation and config examples.

.mcp.json.example names the channel MCP server entry "claude-channel", and README.md:142 tells users to launch with "--dangerously-load-development-channels server:claude-channel". But channel.py hardcodes SERVER_NAME = "fast-mcp-claude-channel", and channel.py:98-99 auto-allows the channel's own reply tool by that exact fully-qualified name: mcp__fast-mcp-claude-channel__reply.

Under the example's key, the reply tool would register as mcp__claude-channel__reply instead, which would not match the hardcoded auto-allow entry. start-session.sh:192 already uses the correct "fast-mcp-claude-channel" key, so the production path may be unaffected — this needs to be verified specifically against someone following .mcp.json.example + README.md literally, since that is a documented, user-facing path.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Confirmed whether the mcp.json.example channel server key mismatch actually breaks the reply tool auto-allow when followed literally
- [ ] #2 If broken: the example key matches channel.py SERVER_NAME, or the auto-allow logic is made robust to the configured server name, with a test or documented manual repro
- [ ] #3 If not exploitable in practice: the reasoning is recorded on the task and no functional change is made
<!-- AC:END -->
