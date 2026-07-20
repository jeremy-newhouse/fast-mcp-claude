# Handover — resolve FMC-8, .mcp.json.example channel server-key mismatch

**Date**: 2026-07-20 | **Grounded against**: `dev` @ `309912ffc0ab8e0bcc32be482815b4826fcbcb46`, clean working tree, 1 commit ahead of `origin/dev` (this archive-move commit; push it as part of R5 before ending this session) | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-8 — Verify and fix .mcp.json.example channel server-key mismatch vs
channel.py's hardcoded SERVER_NAME (Medium priority, channels/config; 3 acceptance
criteria, conditionally structured — see below). Queue order confirmed by user on
2026-07-20 (docs first, then High->Medium->Low severity: FMC-7(done) -> FMC-3(done)
-> FMC-2(done) -> FMC-8 -> FMC-4 -> FMC-5 -> FMC-6); do not re-ask.

Session 3 resolved FMC-2 (who() announce_token leak) and merged it into dev via PR
#23 (rebase-merge, commits e6be632 + 2c809ff + a07e02f). Ran an independent
adversarial-review subagent on the branch diff before opening the PR (per the
tracker's "riskiest/security" judgment call, same as session 2's approach) — it
found 2 non-blocking issues (shallow redaction missed nested metadata dicts;
store.list_presence() had no warning docstring for future callers), both fixed in
a follow-up commit on the same branch before the PR was opened, re-verified, then
merged. No repeat of the sessions 1/2 PR-merge ordering-gap bug this time: ran
`git status --porcelain` immediately before `gh pr merge` (clean), and confirmed
`git log origin/dev --oneline` afterward contained all 3 expected commits.
```

## State

| Item | Status |
| --- | --- |
| Tracker doc | doc-1, cursor advanced to FMC-8, FMC-2 moved to Resolved with evidence |
| FMC-2 | Done — merged to `dev` via PR #23 (rebase-merge, commits `e6be632`+`2c809ff`+`a07e02f`) |
| Cursor issue | FMC-8 (queue position 1 of 4 remaining), status: To Do |
| Queue order | FMC-8 → FMC-4 → FMC-5 → FMC-6 |
| Branch | `dev` (this repo's campaign default branch — not `main`) |
| Working tree | Clean as of `309912f` |
| Remote sync | `dev` is 1 commit AHEAD of `origin/dev` (the handover-archive commit `309912f`) — **push it** (`git push origin dev`) before/as part of starting the next session; R5's own protocol requires this push unconditionally |
| `feature/*` branches | None (local or remote — `feature/FMC-2` deleted both sides via `gh pr merge --delete-branch`, confirmed via `git ls-remote --heads origin feature/FMC-2` returning nothing) |
| Open PRs | None (`gh pr list --state open` empty) |
| `.claude/handovers/` | This file is the only active one; the FMC-2 handover was archived to `archive/handovers/HANDOVER-2026-07-20-backlog-campaign-3.md` (name collision with existing `-2` and unsuffixed entries at the same date+topic — suffixed `-3`), committed (`309912f`) |

## Next steps

1. **First action of the session, before anything else**: `git push origin dev` — the previous session's archive commit (`309912f`) is sitting unpushed locally. (If you're reading this from a fresh clone/session, `git status -b` will already show `ahead 1`; push it as step 0 of preflight.)
2. Run the per-issue lifecycle on FMC-8: `git checkout -b feature/FMC-8 dev`, read `backlog instructions task-execution`, mark FMC-8 In Progress + assign `@claude`, record an implementation plan.
3. FMC-8's issue (verified current at `dev @ 309912f` per the task description — read the files fresh anyway, this task is explicitly about verifying a claim, not assuming it):
   - `.mcp.json.example` names the channel MCP server entry `"claude-channel"`.
   - `README.md:142` tells users to launch with `--dangerously-load-development-channels server:claude-channel`.
   - `channel.py` hardcodes `SERVER_NAME = "fast-mcp-claude-channel"`, and (per the task) `channel.py:98-99` auto-allows the channel's own `reply` tool by the exact fully-qualified name `mcp__fast-mcp-claude-channel__reply`.
   - Under the example's key (`claude-channel`), Claude Code would presumably register the reply tool as `mcp__claude-channel__reply` instead — which would NOT match the hardcoded auto-allow entry.
   - `start-session.sh:192` reportedly already uses the correct `"fast-mcp-claude-channel"` key — so the production/documented launch path via `start-session.sh` may be unaffected; only someone following `.mcp.json.example` + README.md **literally** (bypassing `start-session.sh`) would hit this.
4. This task is structured as a **verify-first** bug (unusual in this queue — FMC-2/FMC-3 were straight fixes): AC #1 asks you to CONFIRM whether the mismatch is actually exploitable before deciding AC #2 vs AC #3:
   - AC #1: Confirmed whether the mismatch actually breaks the reply-tool auto-allow when followed literally.
   - AC #2 (if broken): fix the example key to match `channel.py`'s `SERVER_NAME`, OR make the auto-allow logic robust to the configured server name — with a test or documented manual repro.
   - AC #3 (if NOT exploitable in practice): record the reasoning on the task and make NO functional change.
   - Don't force a code fix if the honest finding is "not exploitable" — AC #3 explicitly wants a no-op outcome to be an acceptable, fully-resolved result in that case. This is a genuine branch point, not a formality — read `channel.py`'s actual auto-allow implementation (not just the task's line-number claims, which may have drifted) before concluding either way.
5. Depending on which branch (AC #2 or AC #3) the investigation lands on, the commit/PR content will differ substantially (a real code+test change vs. a task-notes-only recorded finding) — plan accordingly, but the lifecycle steps (branch → tracker update → commit → review → PR → merge → prune → re-arm) are the same either way.
6. Continue the lifecycle: tracker update on branch (advance cursor to FMC-4, move FMC-8 to Resolved, session-log entry) → commit → **`git status --porcelain` check immediately before `gh pr merge`** (this discipline caught real bugs in sessions 1+2 and was clean in session 3 — keep doing it every session, not just when something feels risky) → review (`git diff dev...HEAD`; this is a config/docs verification task, lower risk than FMC-2's security fix, so self-review is likely sufficient unless AC #2's branch requires an actual code change to the auto-allow logic, in which case consider an adversarial subagent pass same as sessions 2 and 3) → push → PR → merge → **verify `origin/dev`'s log actually contains every commit you made** → prune → re-arm.

## Critical context / traps

- **This repo's campaign default branch is `dev`, not `main`** — same as every prior session; `main` is a separate downstream branch this campaign does not touch unless asked.
- **The PR-merge / trailing-commit ordering gap that bit sessions 1 and 2 did NOT recur in session 3** — the `git status --porcelain` check immediately before `gh pr merge` (not just before the last commit) is the fix; keep doing it every session regardless of how the session "feels."
- FMC-8 is the first **verify-first** task in this queue (AC #1 requires confirming exploitability before choosing whether AC #2 or AC #3 applies) — don't skip the confirmation step and jump straight to "fixing" the example key; the task explicitly allows a no-change outcome if the reasoning is sound and recorded.
- Queue issues FMC-4/FMC-5/FMC-6 were all independently reviewed in session 0 (init) and judged agent-resolvable; nothing is in the tracker's "Not queued" section.
- FMC-5 (later in the queue, after FMC-4) is flagged in the tracker as the riskiest remaining change — touches core long-poll infra (`services/store.py`'s `Notifier`). Not relevant yet for FMC-8, but worth noting for when the cursor reaches it — that will likely be the next session warranting an adversarial-review pass regardless of self-assessed risk.

## Do not repeat

- Don't batch a `backlog task edit` (or any file mutation) with a `git commit` in the same parallel tool-call round unless you've explicitly staged that exact file in that exact commit — verify with `git status --porcelain` immediately before committing, not just before staging.
- Don't run `backlog task edit --append-notes` (e.g. to record a post-review finding) as an afterthought once the branch is already pushed and the PR is about to be merged — fold that note into the same commit/push cycle as the review itself, before triggering the merge, so there's no window for a dangling uncommitted edit. (Session 3 did this correctly: the review-driven fixes + their task note were committed together, then pushed, then the PR was opened.)
- Don't assume a bug-shaped task necessarily needs a code fix — FMC-8 is explicitly structured to allow "confirmed not exploitable, no change made" as a valid, fully-resolved outcome (AC #3). Forcing a fix where none is warranted would be scope creep in the opposite direction.
