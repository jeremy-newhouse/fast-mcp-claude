---
id: FMC-18
title: >-
  Onboard Codex CLI sessions as mesh peers: config contract plus worker and
  control skills
status: Done
assignee:
  - '@claude'
created_date: '2026-08-03 01:03'
updated_date: '2026-08-03 13:22'
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
- [x] #1 A documented config.toml snippet registers the local mesh server and at least one remote peer, including default_tools_approval_mode and tool_timeout_sec
- [x] #2 The docs state the headless auto-deny trap, the 300s tool timeout, and that the mesh bearer is readable from the Codex process env unless shell_environment_policy filters it
- [x] #3 Codex-side equivalents of the worker and control skills exist (Codex skills, prompts or AGENTS.md) and are discoverable from README
- [x] #4 End-to-end verified: a Claude controller send_prompt reaches a Codex session, the Codex session replies, and the controller wait_for_completion returns the reply
- [x] #5 End-to-end verified in reverse: a Codex session drives a Claude worker to completion
- [x] #6 README and CLAUDE.md describe the Codex peer role alongside the existing Claude roles
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Empirically verify whether Codex discovers project-level .codex/skills/ (per doc-3's warning not to assume from the hooks finding) via a throwaway probe skill inside vs outside the repo.
2. Write README.md 'Wire up Codex CLI' section: config.toml snippet (url, bearer_token_env_var, default_tools_approval_mode=approve, tool_timeout_sec) plus the three traps (auto-deny, 300s timeout, bearer-in-env); add the same caveat to the Security section (AC#1, #2).
3. Write .codex/skills/codex-worker/SKILL.md and .codex/skills/codex-control/SKILL.md mirroring .claude/commands/worker.md and control.md, adapted to Codex's tool-name-prefix (hyphen->underscore) and higher tool_timeout_sec; verify live that Codex actually loads them (AC#3).
4. Add a short 'Codex peers' note to CLAUDE.md's Architecture section plus a Known-limitations bullet on the hooks-vs-skills discovery asymmetry (AC#6).
5. AC#4 E2E: register the mesh server as a codex mcp server (fmc-test, approve mode); run a live codex exec pull-mode worker (wait_for_instruction/reply) against a real headless 'claude -p --mcp-config ... --strict-mcp-config' controller (send_prompt/wait_for_completion); confirm the reply round-trips.
6. AC#5 E2E: run a live codex exec controller (send_prompt/wait_for_completion) against the already-online fast-mcp-claude-launcher pm2 worker (a real Claude worker); discover and document the launcher's JSON task-envelope requirement; confirm it drives a real claude -p subprocess to completion.
7. Clean up all throwaway state (probe skill, fmc-test mcp registration) before finalizing.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Empirically verified (codex-cli 0.145.0, two codex exec probes: inside vs outside the repo) that project-level <repo>/.codex/skills/ IS auto-discovered via the model's skill list -- the OPPOSITE of doc-3 finding 6 for project-level .codex/hooks.json, which is NOT discovered. So codex-worker/codex-control skills can ship at <repo>/.codex/skills/ for zero-install discoverability, same ergonomics as .claude/commands/ for Claude Code.

AC#4 verified live: a real headless Claude controller (claude -p --mcp-config ... --strict-mcp-config, targeting claude-local) called send_prompt(recipient_session=codex-e2e-worker); a live codex exec pull-mode worker (registered as mcp server fmc-test, default_tools_approval_mode=approve) received it via wait_for_instruction and called reply; the controller's wait_for_completion returned that exact reply. Full round trip via real tool calls on both sides, not code inspection.

AC#5 verified live: a live codex exec controller (registered mesh server fmc-test, approve mode) called send_prompt(recipient_session=mini2_launcher) targeting the already-online fast-mcp-claude-launcher pm2 worker, then wait_for_completion. First attempt used a plain-text prompt and got back {ok:false, error:bad_envelope} -- discovered live that the launcher requires prompt to be a JSON task envelope ({task, cwd, ...}), unlike the interactive/channel worker contract which takes plain text. Documented this trap in .codex/skills/codex-control/SKILL.md. Retried with the correct envelope: the launcher spawned a real claude -p subprocess (claude_session_id fa71d9b8-422b-4c02-9101-5ee61b1e5934, cost_usd 0.0867, duration_s 12.8) that read README.md and returned its actual first line (# fast-mcp-claude) with ok:true/exit_code:0. Removed the throwaway fmc-test mesh-server registration from ~/.codex/config.toml afterward (codex mcp remove fmc-test) to leave the machine's global codex config clean.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Delivered Phase 0 of doc-3's Codex mesh-support phasing: config contract + skills, zero server code changes. AC#1/#2/#6: README.md's new 'Wire up Codex CLI' section documents the ~/.codex/config.toml [mcp_servers.NAME] snippet (url, bearer_token_env_var, default_tools_approval_mode=approve, tool_timeout_sec) plus the auto-deny trap, the 300s timeout ceiling, the hyphen->underscore tool-name-prefix gotcha, and the bearer-in-process-env caveat (also added to the Security section); CLAUDE.md gained a Codex-peers Architecture note plus a Known-limitations bullet on the hooks-vs-skills discovery asymmetry. AC#3: .codex/skills/codex-worker/SKILL.md and .codex/skills/codex-control/SKILL.md mirror .claude/commands/worker.md/control.md; confirmed live via codex exec (0.145.0) that project-level .codex/skills/ IS auto-discovered (the opposite of doc-3's hooks.json finding) and that both skills load and are matched by description. AC#4/#5: verified bidirectionally with real live tool calls, not code inspection -- a headless 'claude -p' controller's send_prompt reached a live codex exec pull-mode worker, which replied, and wait_for_completion returned it; and a live codex exec controller's send_prompt drove the already-online fast-mcp-claude-launcher pm2 worker (a real Claude worker) to completion via a real claude -p subprocess, after discovering and documenting the launcher's JSON task-envelope requirement (a real trap hit live, not anticipated from docs). All throwaway state (probe skill, fmc-test mcp registration) removed before finalizing. No src/ changes; ruff check clean.
<!-- SECTION:FINAL_SUMMARY:END -->
