# Handover — resolve FMC-4, sandbox path-existence oracle + missing body-size caps

**Date**: 2026-07-20 | **Grounded against**: `dev` @ `4161867400454634ea1db50f8f22825c37ed2c66`, clean working tree, 1 commit ahead of `origin/dev` (this archive-move commit; push it as part of R5 before ending this session) | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-4 — Close sandbox path-existence oracle + extend body-size caps to structured
fields (Medium priority, security/sandbox; 3 acceptance criteria — see below). Queue order
confirmed by user on 2026-07-20 (docs first, then High->Medium->Low severity: FMC-7(done)
-> FMC-3(done) -> FMC-2(done) -> FMC-8(done) -> FMC-4 -> FMC-5 -> FMC-6); do not re-ask.

Session 4 resolved FMC-8 (.mcp.json.example channel server-key mismatch) and merged it
into dev via PR #24 (rebase-merge, commits 857be89 + 14b5072 + c0bb70e). AC#1 required
CONFIRMING exploitability, not assuming it: verified via a claude-code-guide research
subagent (official Claude Code docs) AND direct `strings` inspection of the installed
`claude` CLI v2.1.216 binary's compiled source that Claude Code names MCP tools by the
.mcp.json config KEY, never the server's self-declared handshake name — confirming the
mismatch was real and broke the channel adapter's reply-tool auto-allow when the example
config was followed literally. Fixed 4 doc/config files + added 3 regression tests. Found
and fixed one MORE stale "claude-channel" reference in README.md during self-review
(missed in the first sweep) as a small follow-up commit before opening the PR — re-grep
broadly for any string being renamed, not just the locations named in the task
description. `git status --porcelain` immediately before `gh pr merge` was clean; `git
log origin/dev --oneline` afterward contained all 3 expected commits.
```

## State

| Item | Status |
| --- | --- |
| Tracker doc | doc-1, cursor advanced to FMC-4, FMC-8 moved to Resolved with evidence |
| FMC-8 | Done — merged to `dev` via PR #24 (rebase-merge, commits `857be89`+`14b5072`+`c0bb70e`) |
| Cursor issue | FMC-4 (queue position 1 of 2 remaining), status: To Do |
| Queue order | FMC-4 → FMC-5 → FMC-6 |
| Branch | `dev` (this repo's campaign default branch — not `main`) |
| Working tree | Clean as of `4161867` |
| Remote sync | `dev` is 1 commit AHEAD of `origin/dev` (the handover-archive commit `4161867`) — **push it** (`git push origin dev`) before/as part of starting the next session; R5's own protocol requires this push unconditionally |
| `feature/*` branches | None (local or remote — `feature/FMC-8` deleted both sides via `gh pr merge --delete-branch`; a stale local remote-tracking ref for it lingered until `git fetch --prune`, confirmed truly gone via `git ls-remote --heads origin feature/FMC-8` returning nothing) |
| Open PRs | None (`gh pr list --state open` empty) |
| `.claude/handovers/` | This file is the only active one; the FMC-8 handover was archived to `archive/handovers/HANDOVER-2026-07-20-backlog-campaign-4.md` (name collision with existing unsuffixed/`-2`/`-3` entries at the same date+topic — suffixed `-4`), committed (`4161867`) |

## Next steps

1. **First action of the session, before anything else**: `git push origin dev` — the previous session's archive commit (`4161867`) is sitting unpushed locally. (If you're reading this from a fresh clone/session, `git status -b` will already show `ahead 1`; push it as step 0 of preflight.)
2. Run the per-issue lifecycle on FMC-4: `git checkout -b feature/FMC-4 dev`, read `backlog instructions task-execution`, mark FMC-4 In Progress + assign `@claude`, record an implementation plan.
3. FMC-4 has two independent sub-fixes (read the task fresh — `backlog task view FMC-4 --plain` — line numbers below are as of `dev @ 4161867`, verify current before editing):
   - **Sub-bug 1 (existence oracle)**: `utils/validation.py:160-161` — the `must_exist` check runs BEFORE the workspace-root containment check in `validate_workspace_path`, so `read_file("/etc/shadow")` vs `read_file("/etc/nope")` return distinguishable errors (and the error message echoes the resolved path), letting an authenticated peer probe for the existence of arbitrary absolute paths outside `WORKSPACE_ROOTS`. Fix is to swap the check order — containment first, existence second. The task explicitly notes the symlink-escape logic itself (`config.py:232`, `resolve(strict=False)` against pre-resolved roots) is sound and NOT part of this bug — don't touch it.
   - **Sub-bug 2 (missing body-size caps)**: CLAUDE.md documents caps (prompt ≤1MB, response ≤4MB, file ≤10MB, pubsub payload ≤256KB) enforced in `utils/validation.py`, but 6 structured fields get `json.dumps`'d straight into SQLite with NO size enforcement: `send_prompt`'s `metadata` (`tools/messaging.py:69-74`), `request_approval`'s `tool_input`/`tool_name` (`tools/permissions.py:50`), `announce`'s `metadata` (`tools/presence.py:61`), `request_session_op`'s `payload` (`tools/session_relay.py:69`), `complete_session_op`'s `result` (`tools/session_relay.py:125`), `request_teams_send`'s `metadata` (`tools/teams_outbox.py:72`). All 6 need an explicit size cap — decide on a consistent limit (the task doesn't specify one; the existing pubsub 256KB cap in `utils/validation.py` is a reasonable precedent for metadata/payload-shaped fields, but check what each field's role suggests before picking a number) and apply it uniformly.
4. Acceptance criteria: #1 (existence-check ordering fixed), #2 (all 6 listed fields capped), #3 (both fixes covered by tests) — all three are straight fixes with no verify-first branching this time (unlike FMC-8).
5. This is a **security-labeled** task (labels: `security`, `sandbox`) touching the sandbox boundary and multiple tool modules — higher risk than FMC-8's docs/config fix. Follow session 2/3's approach: run an independent adversarial-review subagent on the branch diff before opening the PR, not just self-review.
6. Continue the lifecycle: tracker update on branch (advance cursor to FMC-5, move FMC-4 to Resolved, session-log entry) → commit → **`git status --porcelain` check immediately before `gh pr merge`** (this discipline caught real bugs in sessions 1+2, stayed clean in sessions 3+4 — keep doing it every session) → adversarial review (`git diff dev...HEAD`) → push → PR → merge → **verify `origin/dev`'s log actually contains every commit you made** → prune → re-arm. When doing final review, re-grep broadly for every renamed/changed identifier across the whole repo, not just the files the task description named — session 4 missed one reference on the first pass and caught it only via a repo-wide grep before opening the PR.

## Critical context / traps

- **This repo's campaign default branch is `dev`, not `main`** — same as every prior session; `main` is a separate downstream branch this campaign does not touch unless asked.
- **The PR-merge / trailing-commit ordering gap that bit sessions 1 and 2 did NOT recur in sessions 3 or 4** — the `git status --porcelain` check immediately before `gh pr merge` (not just before the last commit) is the fix; keep doing it every session regardless of how the session "feels."
- FMC-4's two sub-bugs are independent — you can fix and test them separately, but both need to land before checking AC#3 ("both fixes are covered by tests").
- Don't touch the symlink-escape resolution logic in `config.py:232` / `validate_workspace_path`'s `resolve(strict=False)` handling — the task explicitly calls that part sound; the bug is purely check-ordering.
- No existing precedent in this repo for a single "size cap" helper shared across fields (checked during FMC-8 session's adjacent exploration) — you may need to introduce one in `utils/validation.py` alongside the existing prompt/response/file/pubsub caps, or reuse an existing pattern if you find one on a closer read.
- FMC-5 (next in queue after FMC-4) is flagged in the tracker as the riskiest remaining change — touches core long-poll infra (`services/store.py`'s `Notifier`). Not relevant yet for FMC-4, but worth noting for when the cursor reaches it.

## Do not repeat

- Don't batch a `backlog task edit` (or any file mutation) with a `git commit` in the same parallel tool-call round unless you've explicitly staged that exact file in that exact commit — verify with `git status --porcelain` immediately before committing, not just before staging.
- Don't run `backlog task edit --append-notes` (e.g. to record a post-review finding) as an afterthought once the branch is already pushed and the PR is about to be merged — fold that note into the same commit/push cycle as the review itself, before triggering the merge, so there's no window for a dangling uncommitted edit.
- Don't trust a single grep pass when renaming/fixing a string that's documented in multiple places — FMC-8's first fix pass missed one prose reference to the old channel key in README.md; a repo-wide `grep -rn` for the old value (excluding the new value) before opening the PR caught it. Do this sweep for FMC-4 too if you rename or introduce a shared constant (e.g. a size-cap value).
- Don't assume a bug-shaped task necessarily needs a code fix without checking — FMC-8 was explicitly structured to allow a no-op outcome, and confirming exploitability first (via independent verification, not just trusting the task description or an in-code comment) was the right call before touching any files. FMC-4 has no such branch (it's a straight fix), but the verify-before-fix discipline is still worth applying to the specific cap VALUES you choose, since the task doesn't hand you a number to use.
