# Handover — add a codex exec headless worker engine to the launcher (FMC-19)

**Date**: 2026-08-03 (UTC) | **Grounded against**: `dev` @ `f2b48fc`, working tree clean, 1 ahead / 0 behind `origin/dev` (the archive-handover commit below, about to be pushed) | **Tracker**: `doc-1`

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-19 — add a `codex exec` headless worker engine to the launcher (depends on
FMC-18, now Done). Queue order (FMC-17 → 18 → 19) confirmed by the user on 2026-08-02; do
not re-ask. This is the LAST item in the queue — after it, the campaign is complete
pending user direction on a fresh init.

Integration branch is dev, NOT main. Read backlog/docs/research/doc-3 (§2's launcher.py
row, §6's security notes, §7's phase-1 scope) before planning — it maps `codex exec`'s
actual CLI surface (verified on codex-cli 0.145.0: prompt via argv/stdin, --json JSONL
events, -o/--output-last-message, --output-schema, -C/--cd, --add-dir, --sandbox
{read-only,workspace-write,danger-full-access}, --ephemeral, --ignore-user-config, exec
resume --last|<id>). FMC-18 (this session) shipped the config+skills half with ZERO
launcher.py changes; FMC-19 is the first PHASE 1 issue — it touches real code
(src/fast_mcp_claude/launcher.py) for the first time in this campaign's Codex work.
codex-cli 0.145.0 is installed; the mesh server is live on 127.0.0.1:5473 under pm2
(restart after any server-side change: `pm2 restart fast-mcp-claude`; there's also a
distinct `fast-mcp-claude-launcher` pm2 process — restart THAT after any launcher.py
change, not the server). MCP_API_KEY is in .env — export it, never print it.
```

## State

| Item | Status |
| --- | --- |
| Branch / HEAD | `dev` @ `f2b48fc`, clean, 1 commit ahead of `origin/dev` (this handover's own archive commit — push it as part of this restore's re-arm, or it'll already be pushed if this handover's own session did it) |
| Cursor issue | **FMC-19** — To Do, unassigned, 6 ACs, no plan recorded yet, depends on FMC-18 (now Done) |
| Queue | FMC-19 only — the LAST item |
| Resolved so far | 17 issues (15 from the first two passes + FMC-17 + FMC-18) |
| Feature branches | none, local or remote |
| Open PRs | none |
| Tracker | `doc-1`, cursor + queue + session-19 log entry + FMC-18 Resolved row committed on `dev` (`c6d0fe4`, part of the FMC-18 merge) and pushed |
| Mesh server | pm2 `fast-mcp-claude` last restarted during FMC-17 (session 18); FMC-18 (this session) made zero `src/` changes so no restart was needed. FMC-19 WILL touch `src/fast_mcp_claude/launcher.py` — restart the `fast-mcp-claude-launcher` pm2 process (not `fast-mcp-claude`) after implementing, and re-verify the already-online launcher lane still behaves correctly for the Claude engine (regression risk: FMC-19's own AC#5 requires this) |
| Handovers dir | this file only; 18 consumed handovers in `archive/handovers/` (this session archived the session-18 handover as `HANDOVER-2026-08-03-backlog-campaign-2.md` — a `-2` suffix because the session-17 handover it followed was ALSO dated 2026-08-03 in its filename, a pre-existing collision in the archive; check for another collision before archiving this one too) |

## Next steps

1. Preflight (clean tree, sync `dev`), then `git checkout -b feature/FMC-19 dev`.
2. `backlog instructions task-execution`; view FMC-19 (`backlog task view FMC-19 --plain`); mark In Progress + assign; record the implementation plan on the task. Re-read doc-3's launcher.py row (§2), the security notes (§6, specifically the `approve_tool` self-approval risk and `shell_environment_policy`), and read `src/fast_mcp_claude/launcher.py` in full — it's a large, carefully-hardened file (FMC-15/FMC-16 fixed real concurrency/security bugs in it) and FMC-19 must preserve every existing invariant while adding a second engine.
3. AC#1: add a pluggable engine selection (per-task or per-launcher-instance — read the task's own "Mapping notes" for the recommended shape) so `codex exec` can be spawned alongside the existing `claude -p` path, with the SAME `cwd_allowlist` and concurrency-limit enforcement. Consider whether the existing `TaskEnvelope`/`parse_envelope` (the JSON task-envelope contract FMC-18 discovered live: `{"task": ..., "cwd": ..., ...}`) needs an `engine` field, and whether that's per-task or fixed per launcher instance — this is a real design decision, not a given; the task's own note says compare against `codex mcp-server` (which exposes `codex`/`codex-reply` as MCP tools, letting a controller drive a Codex conversation with NO launcher process) before committing to porting the launcher pattern verbatim.
4. AC#2: the Codex engine's reply must use the exact same response shape as the Claude engine (`{"ok", "exit_code", "timed_out", "duration_s", "result", "stderr_tail", ...}` — see the real live reply shape captured during FMC-18's AC#5 test) including on failure/timeout. Reuse `_read_capped`/`_drain_capped` (FMC-16) for bounded output, `_kill_group`/`_group_alive` (FMC-16) for process-group teardown — `codex exec` is a subprocess like `claude -p`, so these should port directly.
5. AC#3: map the Claude `--tools` ceiling to Codex's sandbox mode + `approval_policy` + per-server `enabled_tools`/`disabled_tools`, per the task's own mapping note. Document the equivalence (there is no 1:1 mapping — sandbox mode is coarser-grained than a tool allowlist).
6. AC#4: **the sharpest security risk in this task** — a Codex worker holding `MCP_API_KEY` can call `approve_tool` and self-approve, defeating the permission relay entirely (doc-3 §6, also noted in FMC-18's new README Security bullet). Reuse the launcher's EXISTING mitigation pattern (the launcher holds the bearer; the worker's hook asks over `CRM_HOOK_SOCKET` instead of holding a mesh credential itself) rather than inventing a new one, and set `shell_environment_policy` so the Codex agent process can't read `MCP_API_KEY` out of its own env even if it tried. Verify the Codex worker process's env directly (e.g. dump `/proc/<pid>/environ` equivalent or have the sandboxed agent try to read the var) rather than trusting config alone — FMC-18 already showed this campaign's value in checking claims live rather than from docs.
7. AC#5: add tests for engine selection and the Codex result path; run the FULL existing launcher test suite to confirm zero Claude-engine regressions (this is explicitly required by the AC, not just good practice).
8. AC#6: document the Codex lane in the launcher's own docs (check `worker-supervisor/README.md` and any launcher-specific doc for the right location) — state exactly which Codex version it was verified against (0.145.0, unless a newer one is available when this runs).
9. Given this is the LAST item in the queue: after resolving, the tracker's queue table will be empty. Follow the "Queue empty" path in R6 of the skill — summarize the full 17-issue Resolved table, archive the final handover, and suggest `init` for a fresh queue rather than writing another one.
10. On the branch: `backlog doc update doc-1` → clear the queue (empty), move FMC-19 into Resolved, append the session-log entry noting campaign completion.
11. Commit (`feat(FMC-19): ...` + `Refs: FMC-19` + the Co-Authored-By/Claude-Session trailers — check recent `git log` for the exact trailer text), review the full diff (self-review or an adversarial subagent — mandatory here given AC#4's security surface, same practice as FMC-9/FMC-11/FMC-12/FMC-14/FMC-15/FMC-16), fix findings, push, `gh pr create --base dev`, `gh pr merge --rebase --delete-branch` (note: this also auto-syncs and prunes your LOCAL checkout — confirmed empirically this session, don't manually re-run steps 9/10 of the lifecycle assuming they're still needed, just verify with `git status`/`git branch -a` that they already happened).

## Critical context / traps

- **Integration branch is `dev`.** `origin/HEAD` points at `main`; every session in this campaign merges into `dev`, not `main`.
- **`gh pr merge --rebase --delete-branch` auto-syncs your local checkout.** Confirmed this session: after merging, the local repo was already switched to `dev`, fast-forwarded, and the local `feature/FMC-18` branch was already gone — `git branch -d feature/FMC-18` returned "not found" because gh had already deleted it. Don't assume lifecycle steps 9/10 need manual action; verify first with `git status`/`git branch -a`, then only act if something's actually left to do. Still run `git fetch --prune` to clear the stale `remotes/origin/feature/<KEY>` tracking ref.
- **The FMC-18 launcher probe found a real, undocumented trap**: `send_prompt`'s `prompt` field to the launcher identity (`<name>_launcher`, e.g. `mini2_launcher`) MUST be a JSON task envelope (`{"task": "...", "cwd": "..."}`), not plain text — a plain-text prompt fails immediately with `{"ok": false, "error": "bad_envelope", ...}`. This is existing launcher.py behavior (`parse_envelope`), not something FMC-18 changed, but it's exactly the code path FMC-19's Codex engine will also go through — the envelope contract doesn't change per-engine, only what spawns after it's parsed.
- **Codex mesh-server registration for live testing is a MACHINE-GLOBAL file** (`~/.codex/config.toml`), not a repo file. FMC-18 registered a throwaway `fmc-test` entry for live E2E testing and removed it afterward (`codex mcp remove fmc-test`) to leave the user's global Codex config clean — do the same for any live testing in FMC-19, and don't leave test entries behind.
- **Codex verification runs bill the user's OpenAI account.** Keep prompts tight; `model_reasoning_effort="low"` was used throughout FMC-18's live probes via `-c 'model_reasoning_effort="low"'`.
- **Project-level `.codex/skills/` IS discovered on 0.145.0; project-level `.codex/hooks.json` is NOT** (verified empirically both ways this session, via a throwaway probe skill created inside vs. outside the repo). This doesn't directly affect FMC-19 (a launcher engine, not a skill or hook), but it's the established fact base for any future Codex-hooks work.
- Backlog records change only through the `backlog` CLI. `doc update --content` **replaces** the whole body — read the file, edit surgically (the Resolved table now holds 17 long rows worth preserving), then pass it back.

## Do not repeat

- **Don't assume a live E2E test's plain-text prompt will reach a launcher identity.** FMC-18's first AC#5 attempt used a plain-text prompt against `mini2_launcher` and failed with `bad_envelope` before any real work happened — always use the JSON task envelope for launcher targets.
- **Don't leave throwaway `~/.codex/config.toml` entries or `.codex/skills/` probe directories around after live testing** — both are easy to create for verification and easy to forget to remove; FMC-18 had to clean up a `zzz-test-marker` probe skill and an `fmc-test` mcp registration before this handover was written.
- **Don't take one Codex empirical finding as proof of a *different* Codex mechanism's behavior.** doc-3 explicitly warned against assuming skills-discovery mirrors hooks-discovery, and the two turned out to be OPPOSITE (skills: project-discovered; hooks: not). Verify each mechanism FMC-19 touches (sandbox modes, `approval_policy`, `enabled_tools`/`disabled_tools`) empirically rather than by analogy to what FMC-17/18 already verified for other mechanisms.
