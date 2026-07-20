# Handover — resolve FMC-2, who() leaks announce_token

**Date**: 2026-07-20 | **Grounded against**: `dev` @ `dae8924d3128f70f9a7aee75913e295ec97ccfaa` (includes the archive-move housekeeping commit; FMC-3's actual merge landed at `740b649`), clean working tree, pushed and in sync with `origin/dev` | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-2 — Fix: who() leaks announce_token, defeating the ECA-71 identity guard
(High priority, security/presence; 3 acceptance criteria). Queue order confirmed by
user on 2026-07-20 (docs first, then High->Medium->Low severity: FMC-7(done) ->
FMC-3(done) -> FMC-2 -> FMC-8 -> FMC-4 -> FMC-5 -> FMC-6); do not re-ask.

Session 2 resolved FMC-3 (auth.py process-global lockout + non-ASCII bearer crash) and
merged it into dev via PR #22. One process note, a REPEAT of the same ordering-gap
class flagged after session 1 (this happened again despite the warning — pay extra
attention this time): after `git push origin feature/FMC-3` + `gh pr create` +
`gh pr merge feature/FMC-3 --rebase --delete-branch`, the merge command failed
locally on its post-merge checkout step (an uncommitted `backlog task edit
--append-notes` change to the FMC-3 task file was sitting in the working tree,
blocking `git checkout dev`). The PR had ALREADY MERGED SERVER-SIDE before that
local checkout failure surfaced. I then committed the pending task-file change and
pushed it to `feature/FMC-3` (a now-already-merged branch) — that commit never made
it into `dev`. Re-running `gh pr merge` just reported "already merged" and
fast-forwarded local `dev`. Caught it by diffing `origin/dev`'s log/file content
against the trailing commit, then `git cherry-pick <sha>` straight onto `dev` and
pushed. Only after that did I delete the stale remote `feature/FMC-3` (recreated by
the post-merge push, so `--delete-branch` hadn't actually removed it).

Root cause pattern (now confirmed twice): a `backlog task edit` call mutates the
task file on disk; if that mutation isn't committed+pushed to the feature branch
BEFORE `gh pr merge` runs, and the merge command's local housekeeping step then
fails on that same dirty file, the PR can still merge successfully server-side —
you just won't know your working tree had a pending edit until the checkout step
chokes on it. Concrete rule for next session: run `git status --porcelain`
immediately before calling `gh pr merge` (not just before your last commit) — if
anything is dirty, commit and push it to the feature branch FIRST, before ever
invoking `gh pr merge`. Do not treat "git status was clean an hour ago" as
sufficient; re-check right before the merge call specifically. And regardless,
still do the post-merge `git log origin/<default> --oneline` sanity check this
handover keeps recommending — it is what caught the gap both times.
```

## State

| Item | Status |
| --- | --- |
| Tracker doc | doc-1, cursor advanced to FMC-2, FMC-3 moved to Resolved with evidence |
| FMC-3 | Done — merged to `dev` via PR #22 (rebase-merge, commits `8c7dc2f`+`c73f626`), plus one directly-cherry-picked follow-up commit `740b649` (see ordering-gap account above) |
| Cursor issue | FMC-2 (queue position 1 of 5 remaining), status: To Do |
| Queue order | FMC-2 → FMC-8 → FMC-4 → FMC-5 → FMC-6 |
| Branch | `dev` (this repo's campaign default branch — not `main`) |
| Working tree | Clean as of `740b649` |
| Remote sync | Pushed — `origin/dev` == local `dev` @ `740b649` |
| `feature/*` branches | None (local or remote — `feature/FMC-3` deleted both sides, the remote copy required an explicit manual delete this session because the post-merge push had recreated it after `--delete-branch` ran) |
| Open PRs | None (`gh pr list --state open` empty) |
| `.claude/handovers/` | This file is the only active one; the FMC-3 handover was archived to `archive/handovers/HANDOVER-2026-07-20-backlog-campaign-2.md` (name collision with the existing FMC-7 archive entry at the same date+topic — suffixed `-2`), committed (`dae8924`) and pushed |

## Next steps

1. Run the per-issue lifecycle on FMC-2: `git checkout -b feature/FMC-2 dev`, read `backlog instructions task-execution`, mark FMC-2 In Progress + assign `@claude`, record an implementation plan.
2. FMC-2's bug (verified current at `dev @ 740b649` — read the files fresh anyway, line numbers may have drifted):
   - `_row_to_presence` in `src/fast_mcp_claude/services/store.py` (~line 1017-1024 per the task description) returns a presence row's `metadata` dict verbatim. Both `channel.py` (~515) and `launcher.py` (~967) put `announce_token` into that metadata when calling `announce()`. Any caller of `who()` (`presence.py` ~121) therefore reads every live session's own identity/mailbox-ownership token straight out of the metadata blob.
   - Attack chain per the task: `who()` → harvest peer X's `announce_token` → `forget(X, token)` → `announce(X, {announce_token: mine})` → attacker now owns X's mailbox identity, and X's real sidecar gets `IDENTITY_LIVE_ELSEWHERE` and disarms its own inbox loop. This defeats the ECA-71/82 identity-guard threat model, which is specifically about a second process holding the same credential racing to claim an identity.
   - `logging_config.py` (~line 39) already redacts `*_token`-suffixed field names in logs — the codebase already treats these as sensitive; `who()` is the one place that still leaks one over the wire.
3. Likely fix shape (verify against actual code, don't assume): strip/redact `announce_token` (and any other `*_token` key) from the `metadata` dict specifically in the `who()`-facing path — either in `_row_to_presence` (if that function is ONLY used for external-facing reads) or in `presence.py`'s `who()` tool itself (if `_row_to_presence` is also used internally where the token IS needed, e.g. by `forget()`'s own guard check — in which case redact at the `who()` boundary, not the shared row-mapper, to avoid breaking internal consumers). Read every caller of `_row_to_presence` before choosing where to redact.
4. AC #2 requires the forget-then-reannounce identity guard to still work correctly after the fix — this means whatever internal code path validates `announce_token` for `forget()`/`announce()` must keep receiving the real token; only the external `who()` response should scrub it. Trace `forget()` in `presence.py` and its guard in `store.py` (~718-727 per the task) to confirm it doesn't go through whatever you redact.
5. AC #3 wants a regression test asserting `who()` output contains no token fields. Check for an existing `tests/test_presence.py` and follow its fixture/style pattern.
6. Continue the lifecycle: tracker update on branch (advance cursor to FMC-8, move FMC-2 to Resolved, session-log entry) → commit → **`git status --porcelain` check immediately before `gh pr merge`, not just before your last commit** → review (`git diff dev...HEAD`, ideally an independent adversarial subagent given this is a security fix touching identity/presence) → push → PR → merge → **verify `origin/dev`'s log actually contains every commit you made** (see the ordering-gap account above — this is the second occurrence, be rigorous this time) → prune → re-arm.

## Critical context / traps

- **This repo's campaign default branch is `dev`, not `main`** — same as every prior session; `main` is a separate downstream branch this campaign does not touch unless asked.
- **PR-merge / trailing-commit ordering gap has now happened TWICE** (sessions 1 and 2) — see the paste-ready prompt above for the full session-2 account, and the concrete rule: check `git status --porcelain` immediately before calling `gh pr merge`, not just before your last commit before that. Any `backlog task edit` call after your last commit but before the merge is a landmine.
- FMC-2 is a genuine security bug fix with real behavior change (like FMC-3, unlike FMC-7's doc-only work) — hold it to the normal test-coverage bar.
- Queue issues FMC-8/FMC-4/FMC-5/FMC-6 were all independently reviewed in session 0 (init) and judged agent-resolvable; nothing is in the tracker's "Not queued" section.
- FMC-5 (later in the queue) is flagged in the tracker as the riskiest remaining change — touches core long-poll infra (`services/store.py`'s `Notifier`). Not relevant yet for FMC-2, but worth noting for when the cursor reaches it.

## Do not repeat

- Don't batch a `backlog task edit` (or any file mutation) with a `git commit` in the same parallel tool-call round unless you've explicitly staged that exact file in that exact commit — verify with `git status --porcelain` immediately before committing, not just before staging.
- After `gh pr merge`, don't assume "PR shows MERGED" means every locally-pushed commit landed — diff your local branch's commit list against the merged base branch's log before trusting the merge is complete. **This has now bitten two sessions in a row** — before invoking `gh pr merge` at all, run `git status --porcelain` one more time as a final gate, not just after your last commit.
- Don't run `backlog task edit --append-notes` (e.g. to record a post-review finding) as an afterthought once the branch is already pushed and the PR is about to be merged — fold that note into the same commit/push cycle as the review itself, before triggering the merge, so there's no window for a dangling uncommitted edit.
