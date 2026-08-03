---
id: FMC-18
title: >-
  Onboard Codex CLI sessions as mesh peers: config contract plus worker and
  control skills
status: To Do
assignee: []
created_date: '2026-08-03 01:03'
labels:
  - codex
dependencies: []
documentation:
  - >-
    backlog/docs/research/doc-3 -
    Codex-CLI-mesh-support-—-feasibility-research-2026-08-02.md
priority: high
type: feature
ordinal: 18000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
A Codex CLI session can already reach the mesh server with zero server changes — verified on codex-cli 0.145.0: a headless `codex exec` run called `who()` over streamable HTTP with bearer auth and got back the live roster. What is missing is the operator-facing half: nothing in the repo says how to wire a Codex session, and there are no Codex-side equivalents of the `/worker`, `/control` and `/fleet-inbox` skills.

Deliver the config contract plus the skills so a Codex session can act as a CONTROLLER (drive Claude or Codex workers via send_prompt / wait_for_completion / approve_tool) and as a pull-mode WORKER (wait_for_instruction then reply), making Codex and Claude sessions interoperable in both directions.

Load-bearing facts from doc-3, all verified on 0.145.0:
- Registration is `[mcp_servers.NAME]` in `~/.codex/config.toml` with `url` plus `bearer_token_env_var` (or `codex mcp add NAME --url ... --bearer-token-env-var ...`). This is the Codex analog of `.mcp.json`.
- `default_tools_approval_mode = "approve"` is REQUIRED for a headless Codex worker. With `auto`, `writes` or `prompt` every mesh call is auto-denied as `user cancelled MCP tool call`. FMC-17 makes the safer `auto` mode viable for read-only tools.
- Codex allows 300s per MCP tool call (configurable via `tool_timeout_sec`), so long-poll waits do NOT need the <=25s chunking that Claude Code stdio idle timeouts force on us. A 45s subscribe call was verified working.
- `bearer_token_env_var` keeps the mesh key in the Codex process env, where the agent shell can read it unless `shell_environment_policy` filters it.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A documented config.toml snippet registers the local mesh server and at least one remote peer, including default_tools_approval_mode and tool_timeout_sec
- [ ] #2 The docs state the headless auto-deny trap, the 300s tool timeout, and that the mesh bearer is readable from the Codex process env unless shell_environment_policy filters it
- [ ] #3 Codex-side equivalents of the worker and control skills exist (Codex skills, prompts or AGENTS.md) and are discoverable from README
- [ ] #4 End-to-end verified: a Claude controller send_prompt reaches a Codex session, the Codex session replies, and the controller wait_for_completion returns the reply
- [ ] #5 End-to-end verified in reverse: a Codex session drives a Claude worker to completion
- [ ] #6 README and CLAUDE.md describe the Codex peer role alongside the existing Claude roles
<!-- AC:END -->
