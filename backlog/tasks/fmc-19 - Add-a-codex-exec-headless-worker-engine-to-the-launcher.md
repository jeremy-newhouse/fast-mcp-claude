---
id: FMC-19
title: Add a codex exec headless worker engine to the launcher
status: To Do
assignee: []
created_date: '2026-08-03 01:04'
labels:
  - codex
  - launcher
dependencies:
  - FMC-18
documentation:
  - >-
    backlog/docs/research/doc-3 -
    Codex-CLI-mesh-support-—-feasibility-research-2026-08-02.md
priority: medium
type: feature
ordinal: 19000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
launcher.py spawns `claude -p` subprocesses for headless mesh tasks. Codex has a feature-equivalent non-interactive mode, so a peer machine can run headless Codex workers in the mesh alongside Claude ones. Verified present on codex-cli 0.145.0: prompt via argv or stdin, `--json` JSONL event stream, `-o/--output-last-message`, `--output-schema`, `-C/--cd`, `--add-dir`, `--sandbox {read-only,workspace-write,danger-full-access}`, `--ephemeral`, `--ignore-user-config`, and `codex exec resume --last|<id>` for multi-turn continuation.

Add a Codex engine to the launcher (per-task or per-instance selection) rather than forking the launcher, so the existing invariants — cwd allowlist, concurrency limits, duplicate-instance ownership, reply-on-reconnect, bounded output buffering, process-group kill (FMC-15, FMC-16) — apply to both engines.

Mapping notes from doc-3:
- The Claude `--tools` ceiling has no single Codex equivalent. It maps onto sandbox mode plus approval_policy plus per-server `enabled_tools`/`disabled_tools` plus execpolicy `.rules` files.
- SECURITY: a Codex worker holding MCP_API_KEY can call `approve_tool` and self-approve, defeating the permission relay. Reuse the existing mitigation — the launcher holds the bearer and the worker hook asks over the unix socket (CRM_HOOK_SOCKET) — and set `shell_environment_policy` so the agent shell cannot read the key out of its own env.
- An alternative worth evaluating during planning: `codex mcp-server` exposes `codex` and `codex-reply` as MCP tools, letting a controller drive a Codex conversation directly with no launcher process. Compare before building.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The launcher can spawn a task as `codex exec`, selected per task or per launcher instance, with cwd allowlist and concurrency limits enforced identically to the Claude engine
- [ ] #2 The Codex engine posts its result back via reply with the same response shape as the Claude engine, including on failure and timeout
- [ ] #3 A tool ceiling equivalent is enforced for Codex workers (sandbox mode plus approval policy) and documented
- [ ] #4 The Codex worker process env excludes the mesh bearer, or any deviation is explicitly documented with its rationale
- [ ] #5 Existing Claude launcher behavior is unchanged and tests cover engine selection plus the Codex result path
- [ ] #6 The launcher README documents how to run a Codex lane and which Codex version it was verified against
<!-- AC:END -->
