# Handover — resolve FMC-3, auth.py lockout + non-ASCII bearer crash

**Date**: 2026-07-20 | **Grounded against**: `dev` @ `0c5cfebf594f34810bc68e4b0dbdd8c61d7c8217`, clean working tree, pushed and in sync with `origin/dev` | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-3 — Fix auth.py: process-global self-perpetuating lockout and non-ASCII
bearer-token crash (High priority, security/auth; 3 acceptance criteria; two independent
bugs in src/fast_mcp_claude/auth.py). Queue order confirmed by user on 2026-07-20 (docs
first, then High->Medium->Low severity: FMC-7(done) -> FMC-3 -> FMC-2 -> FMC-8 -> FMC-4 ->
FMC-5 -> FMC-6); do not re-ask.

Session 1 resolved FMC-7 (doc accuracy sweep) end-to-end and merged it into dev via PR #21.
One process note for this session: the PR merge landed while a trailing task-notes commit
was still only on the feature branch (not included in the merged PR) — it had to be
cherry-picked onto dev directly afterward. Root cause: a `backlog task edit --append-notes`
call landed in the same tool-call batch as a `git add/commit` that didn't include the task
file, so a later "record review evidence" commit was pushed to the feature branch AFTER
`gh pr merge` had already completed server-side (the local checkout-back-to-base step failed
first, on an unrelated uncommitted task-file diff, which is what surfaced the ordering gap).
Lesson for this session: after `gh pr merge --rebase --delete-branch` returns, verify
`git log origin/<default> --oneline` actually contains every commit you intended, especially
if any `backlog task edit` calls happened in the same batch as a commit — don't assume the
merge captured everything that was pushed.
```

## State

| Item | Status |
| --- | --- |
| Tracker doc | doc-1, cursor advanced to FMC-3, FMC-7 moved to Resolved with evidence |
| FMC-7 | Done — merged to `dev` via PR #21 (rebase-merge, commit `cc58752`), plus one directly-cherry-picked follow-up commit `82fa195` and an archive commit `0c5cfeb` |
| Cursor issue | FMC-3 (queue position 1 of 6 remaining), status: To Do |
| Queue order | FMC-3 → FMC-2 → FMC-8 → FMC-4 → FMC-5 → FMC-6 |
| Branch | `dev` (this repo's campaign default branch — not `main`) |
| Working tree | Clean as of `0c5cfeb` |
| Remote sync | Pushed — `origin/dev` == local `dev` @ `0c5cfeb` |
| `feature/*` branches | None (local or remote — `feature/FMC-7` was deleted both sides after merge) |
| Open PRs | None (`gh pr list --state open` empty) |
| `.claude/handovers/` | This file is the only active one; the FMC-7 handover was archived to `archive/handovers/HANDOVER-2026-07-20-backlog-campaign.md` (committed in `0c5cfeb`) |

## Next steps

1. Run the per-issue lifecycle on FMC-3: `git checkout -b feature/FMC-3 dev`, read `backlog instructions task-execution`, mark FMC-3 In Progress + assign `@claude`, record an implementation plan.
2. FMC-3's two bugs, both in `src/fast_mcp_claude/auth.py` (verified current at `dev @ 0c5cfeb` — read the file fresh anyway, don't trust these line numbers blindly after any prior edits):
   - **Process-global self-perpetuating lockout** (`AuthRateLimiter`, lines ~21-64). One failure counter for the whole server (not per-peer/IP). `check_rate_limit()` (~37) returns `False` for everyone while `_lockout_until` is in the future — including the legitimate peer. `record_success()` (~61) is the only thing that clears `_failed_attempts`, but `verify_token` (~75) never reaches it while locked out (`check_rate_limit` returns early at line 76-77). Failure entries live `ATTEMPT_WINDOW=300s` (~18) while `LOCKOUT_DURATION=60s` (~17) — so one more bad request roughly every minute re-triggers the lockout indefinitely, no credential needed. AC #1 wants this fixed so a locked-out attacker can't indefinitely block the legitimate peer — likely needs per-source tracking (there's no per-connection identity available at this layer today — check how `verify_token` is invoked upstream by `fastmcp`/`TokenVerifier` to see what source info, if any, is available) and/or a bound on how long a sustained low-rate attacker can hold the lock (e.g. only counting toward re-lockout once the previous lockout has actually expired, or capping total lockout extension).
   - **`hmac.compare_digest` raises on non-ASCII tokens** (line ~79). `compare_digest(token, self.api_key)` with `str` args requires ASCII; a non-ASCII bearer raises `TypeError` out of `verify_token`, producing an unhandled 500 instead of a 401, and `record_failure()` (~80) is skipped on that path so it doesn't even count toward the rate limiter. AC #2 wants a clean 401. Likely fix: catch `TypeError` (or `UnicodeEncodeError` on the internal encode) around the `compare_digest` call, or encode both sides to bytes first (`token.encode()` / `self.api_key.encode()`, which don't raise on non-ASCII) before comparing.
3. AC #3 requires test coverage for both scenarios. `tests/test_auth.py` already exists with a `TestApiKeyVerifier` class (5 tests: valid/wrong/empty/partial-key/case-sensitivity) and a `TestAuthRateLimiter` class (2 tests: `test_lockout_after_max_failures`, `test_success_clears_failures`) — add new tests there following the existing fixture/style pattern rather than starting a new file.
4. This is a real behavior-changing bug fix (unlike FMC-7's doc-only work) — run `uv run pytest tests/test_auth.py -v` and the full `uv run pytest` + `uv run ruff check src/ tests/` before considering it done.
5. Continue the lifecycle: tracker update on branch (advance cursor to FMC-2, move FMC-3 to Resolved, session-log entry) → commit → review (`git diff dev...HEAD`, ideally an independent adversarial subagent given this is a security fix) → push → PR → merge → **verify the merged commit on `origin/dev` actually contains every commit you made, including any trailing `backlog task edit` commits** (see the ordering-gap lesson above) → prune → re-arm.

## Critical context / traps

- **This repo's campaign default branch is `dev`, not `main`** — same as every prior session; `main` is a separate downstream branch this campaign does not touch unless asked.
- **PR-merge / trailing-commit ordering gap** (see the paste-ready prompt above for the full account): if you run `backlog task edit --append-notes` (or any task-file mutation) in the same tool-call batch as `git commit`, and that commit isn't explicitly staged with the task-file change included, you can end up pushing a commit to the feature branch *after* `gh pr merge` has already merged server-side. Always re-verify `git log origin/dev --oneline` (or equivalent) matches your full local commit list after a merge, before moving on to archive/re-arm steps.
- FMC-3 is a genuine bug fix with real behavior change (unlike FMC-7) — hold it to the normal test-coverage bar, not the doc-only bar FMC-7 used.
- Queue issues FMC-2/FMC-8/FMC-4/FMC-5/FMC-6 were all independently reviewed in session 0 (init) and judged agent-resolvable; nothing is in the tracker's "Not queued" section. Their task descriptions may also carry pre-FMC-7 line numbers for CLAUDE.md-adjacent context (unlikely to matter — they're bug fixes in `src/*.py`, not doc tasks — but re-verify line numbers against current source regardless, per the tracker's standing caution).

## Do not repeat

- Don't batch a `backlog task edit` (or any file mutation) with a `git commit` in the same parallel tool-call round unless you've explicitly staged that exact file in that exact commit — verify with `git status --porcelain` immediately before committing, not just before staging.
- After `gh pr merge`, don't assume "PR shows MERGED" means every locally-pushed commit landed — diff your local branch's commit list against the merged base branch's log before trusting the merge is complete.
