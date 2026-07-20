# Handover — start the FMC-2..FMC-8 security/tech-debt campaign (FMC-7 first)

**Date**: 2026-07-20 | **Grounded against**: `dev` @ `820ab9255046ef05c4e3a82c41c5af7051b2cdff`, clean working tree, pushed and in sync with `origin/dev` | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-7 — Documentation accuracy sweep: fix CLAUDE.md/README.md drift from the
actual implementation (doc-only, no runtime behavior changes; 5 acceptance criteria,
6 clusters of fixes across CLAUDE.md, README.md, spawner/, and start-session.sh header
comments). Queue order confirmed by user on 2026-07-20 (docs first, then High->Medium->Low
severity: FMC-7 -> FMC-3 -> FMC-2 -> FMC-8 -> FMC-4 -> FMC-5 -> FMC-6); do not re-ask.

This session's init also committed and pushed unrelated pre-existing work found
uncommitted at session start: FMC-1's herdr-tmux-shim (4d28753) and the
backlog-handover skill files (e9cc399), both at the user's explicit direction,
kept separate from the campaign's own commits (a5588e4, 279810b, 820ab92).
Everything is on origin/dev — nothing outstanding to push.
```

## State

| Item | Status |
| --- | --- |
| Tracker doc | doc-1 ("Backlog campaign tracker"), created and populated this session |
| Cursor issue | FMC-7 (queue position 1 of 7) |
| Queue order | Confirmed by user 2026-07-20: FMC-7 → FMC-3 → FMC-2 → FMC-8 → FMC-4 → FMC-5 → FMC-6 |
| Branch | `dev` (this repo's campaign default branch — NOT `main`, which `origin/HEAD` points to but which this campaign does not touch) |
| Working tree | Clean as of `820ab92` |
| Remote sync | Pushed — `origin/dev` == local `dev` @ `820ab92` |
| `feature/*` branches | None (checked: no leftover branches from a prior lifecycle) |
| Open PRs | None checked yet this session (no branch work has started) |
| `.claude/handovers/` | Created, gitignored (verified via `.gitignore` diff, committed in `a5588e4`) |
| `archive/handovers/` | Created, tracked via `.gitkeep` (committed in `279810b`) |

## Next steps

1. Run the per-issue lifecycle on FMC-7: `git checkout -b feature/FMC-7 dev`, read `backlog instructions task-execution`, mark FMC-7 In Progress + assign, record an implementation plan.
2. FMC-7's 6 clusters (full detail in `backlog task view FMC-7 --plain`):
   - Permission-relay self-contradiction: CLAUDE.md:119 says implemented, CLAUDE.md:136 (Known Limitations) says not yet implemented; README.md:26/149/233 also carry the stale claim. Reality: implemented (`start-session.sh:20-22,220-222` depends on it). Fix all 4 locations to agree.
   - CLAUDE.md module-layout gaps: `launcher.py` (1551 lines, largest/most security-sensitive module, absent from Module layout ~lines 38-54), `session.py` (405 lines, referenced 4x but never introduced), `session_hook.py` (82 lines) — all missing. Also `presence.forget` undocumented + missing from `server.py:85`'s `instructions` string; `session_relay._VALID_OPS` includes `"check"` (session_relay.py:39) but tool descriptions only mention list/send; CLAUDE.md:126 claims literal `".."` traversal blocking but code relies on `resolve()` instead.
   - Root README.md tooling coverage: only `herdr-tmux-shim/` documented among standalone tooling dirs — `worker-supervisor/`, `spawner/`, `sandbox-runner/`, `start-session.sh` unmentioned. Channel notification source attribute shown as `fast-mcp-claude` (README.md:145) vs actual `fast-mcp-claude-channel`. CLAUDE.md:9 "no central hub" should say no hub is *required*. Tool reference table (~188-211) omits 8 teams_outbox.py/session_relay.py tools.
   - `spawner/` has no README.md (only a pyproject.toml `description =`) while CLAUDE.md:11 claims every tooling dir has one.
   - `start-session.sh` header comment drift (~lines 1-28): identity format example is stale (`<peer>.<repo>` vs actual `<peer>.<repo>.<name-slug>` / `<peer>.<repo>-<hash>` fallback per ADR-0016); env-override list omits `SESSION_NAME`, `SESSION_DESCRIPTION`, `MCP_PORT`; doesn't mention CLI arg passthrough to `exec claude ... "$@"`; line 23 points to a non-existent `docs/channels` dir and cites ADR-0010 vs CLAUDE.md's ADR-0012 for the same feature.
4. This is doc/comment-only work (task description says so explicitly) — no runtime behavior should change. Verify each AC per `backlog instructions task-finalization` (objective: grep/read the actual current file content at the cited line numbers, don't trust the task description's line numbers blindly since the repo has moved since 2026-07-20's review — re-locate each claim first).
5. Continue the lifecycle: tracker update on branch (advance cursor to FMC-3, move FMC-7 to Resolved, session-log entry) → commit → review (`git diff dev...HEAD`) → push → PR → merge → prune → re-arm.

## Critical context / traps

- **This repo's campaign default branch is `dev`, not `main`** — `git symbolic-ref --short refs/remotes/origin/HEAD` resolves to `origin/main`, but CLAUDE.md/this skill's own conventions call out that `main` is a separate downstream branch this campaign does not touch unless asked. All of this session's commits landed on `dev`; the FMC-7 feature branch itself has not been created yet — do `git checkout -b feature/FMC-7 dev`, not off `main`.
- **FMC-7 touches the exact same two files (`CLAUDE.md`, `README.md`) that `4d28753` (FMC-1's herdr-tmux-shim doc pointers) just modified and committed.** Read the current committed state of both files fresh before editing — don't work from the task description's stale line numbers or from any memory of a pre-`4d28753` version.
- Task descriptions for FMC-2..FMC-8 cite line numbers from a 2026-07-20 dogfooding review snapshot; the repo has since gained the herdr-tmux-shim commit (`4d28753`) which shifted some line numbers in CLAUDE.md/README.md. Re-verify line numbers before trusting them, for every queued issue, not just FMC-7.
- All 7 queue issues were independently reviewed this session and judged agent-resolvable (objectively verifiable ACs, no human-at-hardware or product-decision blockers) — nothing is in the tracker's "Not queued" section.
- FMC-1 (Done, unrelated to this campaign) and the `backlog-handover` skill files were also uncommitted at session start; both were committed separately this session (`4d28753`, `e9cc399`) at the user's explicit direction, kept out of the campaign's own tracker commit (`a5588e4`) to keep history clean per-concern.

## Do not repeat

- Nothing failed this session — this is the first init. No prior approaches to avoid yet.
