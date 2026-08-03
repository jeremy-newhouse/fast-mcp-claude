# Handover — onboard Codex CLI as a mesh peer (FMC-18)

**Date**: 2026-08-03 (UTC) | **Grounded against**: `dev` @ `dc5d423`, working tree clean, 1 ahead / 0 behind `origin/dev` (the archive commit below, about to be pushed) | **Tracker**: `doc-1`

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-18 — onboard Codex CLI sessions as mesh peers (config.toml contract +
Codex-side worker/control skills + bidirectional E2E verification). Queue order
(FMC-17 → 18 → 19) confirmed by the user on 2026-08-02; do not re-ask.

Integration branch is dev, NOT main. The research backing this is doc-3
(backlog/docs/research/) — read it before planning; its appendix has the exact
reproduction commands and section 7's phasing table scopes this issue as
"Phase 0": config + skills only, zero server code changes. codex-cli 0.145.0
is installed and the mesh server is live on 127.0.0.1:5473 under pm2 (restart
it after any server-side change: `pm2 restart fast-mcp-claude`). MCP_API_KEY
is in .env — export it, never print it. This is a bigger issue than FMC-17
(High priority, 6 ACs, two live bidirectional E2E checks) — budget accordingly
and don't feel obligated to rush it into one sitting if it doesn't fit; the
campaign's one-issue-per-session rule means it's fine to take the whole
session on just this.
```

## State

| Item | Status |
| --- | --- |
| Branch / HEAD | `dev` @ `dc5d423`, clean, 1 commit ahead of `origin/dev` (archive-handover commit, not yet pushed — push it as part of this restore's re-arm, or it'll already be pushed if this handover's own session did it) |
| Cursor issue | **FMC-18** — To Do, unassigned, 6 ACs, no plan recorded yet |
| Queue | FMC-18 (feature, High) → FMC-19 (feature, depends on FMC-18) |
| Resolved so far | 16 issues (15 from the first two passes + FMC-17 this session) |
| Feature branches | none, local or remote |
| Open PRs | none |
| Tracker | `doc-1`, cursor + queue + session-18 log entry + FMC-17 Resolved row committed on `dev` (`93c7480`, `4ccbf4e`) and pushed |
| Mesh server | pm2 `fast-mcp-claude` restarted twice this session (once for FMC-17's branch code, once after merging to `dev`) — currently serving merged `dev` @ `dc5d423`'s equivalent code (the archive commit doesn't touch `src/`) |
| Handovers dir | this file only; 17 consumed handovers in `archive/handovers/` (this session archived the session-17 handover) |

## Next steps

1. Preflight (clean tree, sync `dev`), then `git checkout -b feature/FMC-18 dev`.
2. `backlog instructions task-execution`; view FMC-18; mark In Progress + assign; record the implementation plan on the task. Re-read doc-3 in full — section 7's phasing table, the "Component mapping" table (§2), and the "Permission-relay deltas" (§2 sub-bullets) are all directly load-bearing for this issue's skills/docs.
3. AC#1/#2 (config contract): write a `~/.codex/config.toml` `[mcp_servers.NAME]` snippet with `url`, `bearer_token_env_var`, `default_tools_approval_mode="approve"` (doc-3 finding 4: `auto`/`writes`/`prompt` all auto-deny headlessly — FMC-17 only narrowed the gap for read-only tools, it did not close it, so `approve` is still the right default here), and `tool_timeout_sec` (300s ceiling vs Claude Code's ~30s stdio idle ceiling). Document the auto-deny trap, the 300s timeout, and that `bearer_token_env_var` leaves the mesh key readable from the Codex process env unless `shell_environment_policy` filters it (doc-3 §6's security note — do not put `MCP_API_KEY` in a Codex WORKER's env if its permission relay is meant to be trustworthy; that's the launcher's job, out of scope here per doc-3's own phasing).
4. AC#3 (skills): Codex's equivalent of `/worker`/`/control`/`/fleet-inbox`. Codex skills live in `$CODEX_HOME/skills` (discoverable via `skill_search`) or `AGENTS.md`/prompts — NOT `<repo>/.codex/` (doc-3 finding 6: project-level `.codex/hooks.json` is NOT discovered on 0.145.0 despite published docs; verify whether the same project-vs-user-home discovery gap applies to skills before assuming `<repo>/.codex/skills` would work — don't take the hooks finding as proof for skills without checking). Make them discoverable from README (this repo's existing `/worker`/`/control`/`/fleet-inbox` are in `.claude/commands/` — mirror that discoverability, adapted to wherever Codex actually finds skills).
5. AC#4/#5 (bidirectional live E2E — the riskiest part): AC#4 needs a live Claude worker (this machine's pm2 `fast-mcp-claude-launcher` lane is online per the session-17 tracker note, or use an interactive Claude worker session) that a Codex controller drives via `send_prompt`/`wait_for_completion`; AC#5 is the reverse — a live Codex session (or `codex exec` in a mode that can call `wait_for_instruction`/`reply`, i.e. a pull-mode worker) driven by a Claude controller. Verify with real tool calls end to end, not code inspection — same discipline FMC-17's AC#3/#4 verification used.
6. AC#6: update README.md (a new section alongside "Wire up Claude Code" / "Use it") and CLAUDE.md (module layout stays Python-only; add a short "Codex peers" note near the Architecture section) describing the Codex peer role.
7. On the branch: `backlog doc update doc-1` → cursor to FMC-19, move FMC-18 into Resolved, append the session-log entry.
8. Commit (`feat(FMC-18): ...` + `Refs: FMC-18` + the Co-Authored-By/Claude-Session trailers this repo uses — check recent `git log` for the exact trailer text), review the full diff (self-review or an adversarial subagent — this campaign's established practice for anything touching trust/security surfaces, and doc-3 §6 flags real ones here), fix findings, push, `gh pr create --base dev`, `gh pr merge --rebase --delete-branch`, sync `dev`, prune the local branch.

## Critical context / traps

- **Integration branch is `dev`.** `origin/HEAD` points at `main`; every session in this campaign merges into `dev`, not `main`.
- **FMC-17 (this session) narrowed but did not close the headless-approval gap.** Read-only tools can now run under `auto`/`writes` mode; mutating tools (including `write_file`, `approve_tool`) still need `approve` or an interactive approval path. Don't assume FMC-17 lets you drop `default_tools_approval_mode="approve"` from the Phase-0 config contract — doc-3's own phasing (§7) still calls for `approve` at Phase 0, before any hook-based relay (Phase 2) exists.
- Export the bearer as `MCP_API_KEY="$(grep -E '^MCP_API_KEY=' .env | head -1 | cut -d= -f2-)"` and never echo it. Codex reads it via `bearer_token_env_var`, i.e. from its own process env.
- Codex verification runs bill the user's OpenAI account. Keep prompts tight and pass `-c 'model_reasoning_effort="low"'`.
- The pm2-managed `fast-mcp-claude` server runs from this exact working directory (`exec cwd` = the repo root), so checking out a feature branch and restarting pm2 serves that branch's code live — used repeatedly this session to verify against a real server. Restart again after merging so `dev`'s merged state is what's actually live (`pm2 restart fast-mcp-claude`).
- Backlog records change only through the `backlog` CLI. `doc update --content` **replaces** the whole body — read the file, edit surgically (the Resolved table now holds 16 long rows worth preserving), then pass it back.
- The initial `uv run pytest` in this session's early minutes looked hung (0 stdout for ~9 minutes through a `| tail -30` pipe, which buffers until EOF) and was killed prematurely — it was NOT hung, just genuinely slow (this suite has real timing/subprocess tests). The kill lost no work but wasted time. If you need to watch a long-running test suite, redirect to a file with `PYTHONUNBUFFERED=1` and tail/grep that file for progress markers instead of piping through `tail` (which shows nothing until the process exits).

## Do not repeat

- **Don't pipe a long-running command through `tail` for progress monitoring** — `tail` (without `-f`) only prints its last N lines at EOF, so a healthy-but-slow process looks identical to a hung one until it finishes. Redirect to a file and tail/grep *that*.
- **Don't assume `.codex/hooks.json` project-level discovery works** (doc-3 finding 6, verified FAIL on 0.145.0 despite published docs describing it) — and don't assume the same is true for skills without checking; verify empirically rather than extrapolating from the hooks finding.
- **Don't put `MCP_API_KEY` in a Codex WORKER's env** if that worker's permission relay is meant to be trustworthy (doc-3 §6) — that's a launcher-mediated-bearer problem for a later phase, not this issue.
