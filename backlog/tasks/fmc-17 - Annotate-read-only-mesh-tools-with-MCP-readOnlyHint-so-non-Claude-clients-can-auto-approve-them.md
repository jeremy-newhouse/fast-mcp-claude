---
id: FMC-17
title: >-
  Annotate read-only mesh tools with MCP readOnlyHint so non-Claude clients can
  auto-approve them
status: To Do
assignee: []
created_date: '2026-08-03 01:03'
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
- [ ] #1 Every mesh tool is classified read-only or not, based on whether it writes to the store or mutates state (not on whether it merely blocks)
- [ ] #2 Read-only tools declare readOnlyHint via FastMCP tool annotations; mutating tools do not
- [ ] #3 A headless `codex exec` run configured with default_tools_approval_mode="auto" calls a read-only mesh tool successfully with no approval prompt
- [ ] #4 The same run still gets an approval request for a mutating tool such as write_file
- [ ] #5 The tool pattern section of CLAUDE.md documents the annotation requirement for new tools
<!-- AC:END -->
