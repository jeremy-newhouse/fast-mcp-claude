# Handover — resolve FMC-6, tool-pattern deviations from CLAUDE.md's documented contract

**Date**: 2026-07-21 | **Grounded against**: `dev` @ `141b7edb4c4c4359b3f2e794b8b168e1551e382f`, clean working tree, 1 commit ahead of `origin/dev` (this handover's archive commit; not yet pushed — R5 pushes it) | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-6 — Fix tool-pattern deviations from CLAUDE.md's documented contract (Low
priority, tech-debt/tools; 3 independent sub-issues, 3 acceptance criteria — see below).
Queue order confirmed by user on 2026-07-20 (docs first, then High->Medium->Low severity:
FMC-7(done) -> FMC-3(done) -> FMC-2(done) -> FMC-8(done) -> FMC-4(done) -> FMC-5(done) ->
FMC-6); do not re-ask. FMC-6 is the LAST item in the queue — if it resolves cleanly this
session, the campaign is complete after it (see backlog-handover skill's "Queue empty"
step in R6).

Session 6 resolved FMC-5 (all 4 Notifier/long-poll correctness bugs in services/store.py)
and merged it into dev via PR #26 (rebase-merge; original commits 6805b2d/6e88fc6/467ea37
rewritten to 5110ac7/4684869/78ea64c by the rebase). Rewrote Notifier together for
sub-bugs 1-3 (notify_prefix/forget/deadline-looping wait_for since they share the class);
wired forget() into _cleanup_once for sub-bug 2's eviction; added a delete_grace param for
sub-bug 4's mark-then-delete fix, scoped to the messages table only after confirming via
existing tests that teams_outbox/session_relay must keep same-pass deletion. Added 7 tests
(284 total, up from 277), each of the 5 regression tests confirmed to fail against the
pre-fix code via git stash. Independent adversarial-review subagent found no blocking
issues; one nitpick (stale Notifier class docstring) was fixed in a follow-up commit
before merging.

FMC-6 is Low severity / tech-debt, lower blast radius than FMC-5 — 3 independent
sub-issues across permissions.py/messaging.py/presence.py, none touching shared
infrastructure. Read the task fresh (`backlog task view FMC-6 --plain`) and re-verify
every line number before editing — this handover's numbers are current as of dev @
141b7ed but may have drifted.
```

## State

| Item | Status |
| --- | --- |
| Tracker doc | doc-1, cursor advanced to FMC-6, FMC-5 moved to Resolved with evidence |
| FMC-5 | Done — merged to `dev` via PR #26 (rebase-merge; commits 5110ac7/4684869/78ea64c on dev, rewritten from the original branch SHAs by the rebase) |
| Cursor issue | FMC-6 (last item in the queue — queue will be empty after this), status: To Do |
| Queue order | FMC-6 only remaining |
| Branch | `dev` (this repo's campaign default branch — not `main`) |
| Working tree | Clean as of `141b7ed` |
| Remote sync | `dev` is 1 commit ahead of `origin/dev` (this handover's archive commit, `141b7ed`) — **R5 pushes it, not yet done as of this writing** |
| `feature/*` branches | None (local or remote — `feature/FMC-5` was deleted both sides automatically by `gh pr merge --delete-branch`, confirmed via `git branch --list` and `git ls-remote --heads origin feature/FMC-5` both empty) |
| Open PRs | None (PR #26 merged) |
| `.claude/handovers/` | This file is the only active one; the FMC-5 handover was archived to `archive/handovers/HANDOVER-2026-07-21-backlog-campaign.md` (no name collision — first `-2026-07-21` entry), committed (`141b7ed`) |

## Next steps

1. Run the per-issue lifecycle on FMC-6: `git checkout -b feature/FMC-6 dev`, read `backlog instructions task-execution`, mark FMC-6 In Progress + assign `@claude`, record an implementation plan.
2. FMC-6 has 3 independent sub-issues plus a doc-accuracy fix (read the task fresh — `backlog task view FMC-6 --plain` — line numbers below are as of `dev @ 141b7ed`, verify current before editing):
   - **Sub-issue 1 (bare `except Exception`, no `ValidationError` branch)**: `pending_approvals` (`tools/permissions.py:103-111`), `get_status` (`tools/messaging.py`, ~line 149 per the task), and `who` (`tools/presence.py`, ~line 128 per the task) each only have `except Exception as e: return format_error_response(e)` with no separate `except ValidationError as e:` branch above it — deviates from CLAUDE.md's documented tool pattern ("Always catch `ValidationError` separately (it produces a 400-style response with `field`)"). Confirmed `pending_approvals` matches exactly: it calls `int(limit)` and clamps it, but never actually raises `ValidationError` itself in the code I read — check whether any of the 3 tools' current bodies can even raise `ValidationError` today (if `pending_approvals` never calls a `validate_*` helper, the "no separate branch" issue may be more about future-proofing/consistency with the documented contract than an active bug with a concrete failure case; `get_status`/`who` need the same check). This affects whether AC#1's fix is a pure mechanical consistency fix or actually changes user-visible error responses for a real failure path — worth confirming before writing tests, since finalization requires objective before/after evidence, not just code presence.
   - **Sub-issue 2 (`wait_for_pending_approval` reaches into `store._notifier` private state)**: `tools/permissions.py:120-143` — the tool does `from ..services.store import Notifier  # noqa: F401  (re-import for clarity)` (a dead import, confirmed present) then calls `await store._notifier.wait_for("approvals:any", lambda: _approvals_or_none(), wait_s)  # type: ignore[attr-defined]` directly instead of exposing this as a `Store` method. Task's suggested fix: mirror the existing `wait_for_pending_teams_sends` pattern (`store.py:522-532` as of `dev @ 141b7ed` post-FMC-5 — re-verify the line numbers shifted since FMC-5 added ~130 lines to store.py) — add a `Store.wait_for_pending_approvals(timeout, limit=50)` method that wraps `self._notifier.wait_for(self._approval_queue_key(), check, timeout)` internally, then have the tool call that instead of touching `_notifier`/`Notifier` directly. Remove the dead `Notifier` import once the direct access is gone.
   - **Sub-issue 3 (misleading timeout `Field` description)**: every long-poll tool's `timeout` parameter description says something like "capped by server's `poll_max_wait_s`" (e.g. `wait_for_pending_approval` at `permissions.py:123`, `await_decision` at `permissions.py:77`), but the actual enforced cap is a hardcoded literal per tool passed to `validate_timeout(..., cap=X)` — 300.0 in most places, 600.0 at `permissions.py:82` (inside `await_decision`). Since the calling model reads these descriptions to decide what timeout to request, and CLAUDE.md's Known Limitations section warns the MCP idle timeout is ~30s for stdio, a description implying an unbounded-by-poll_max_wait_s cap could mislead a caller into requesting a timeout that exceeds the transport's idle window. Fix: grep every `Field(description=...)` near a `validate_timeout(..., cap=...)` call across `tools/*.py` and make each description state its OWN actual cap (e.g. "capped at 300s" / "capped at 600s") instead of the generic `poll_max_wait_s` phrasing — a broader grep than just the 2 examples the task calls out, since AC#3 says "Each tool timeout description", implying all of them, not just permissions.py's two.
3. Acceptance criteria: #1 (all 3 named tools catch `ValidationError` separately), #2 (`wait_for_pending_approval` no longer reaches into `Notifier` internals), #3 (every tool's timeout description accurately states its actual enforced cap) — all need genuine before/after evidence per `backlog instructions task-finalization`. For AC#1 specifically, first determine whether the 3 tools can currently raise `ValidationError` at all (see sub-issue 1 above) — if none of them can today, the "evidence" may need to be a demonstration that adding input validation that raises `ValidationError` now correctly degrades to the structured 400-style response instead of a generic 500, rather than a pre-existing-bug regression test (there may be no live bug to reproduce, only a contract gap to close defensively — that's fine, same "verify-first, no-op is a valid outcome" mode as FMC-8 was, just check before assuming the AC needs a full bug-reproduction test).
4. This is the LAST item in the queue. Once FMC-6 is resolved and merged, the tracker's queue will be empty — follow the skill's R6 "Queue empty" path: summarize the resolved table, archive the final handover (no new one), suggest `init` for a fresh queue if the user wants to continue the campaign with newly-discovered issues.
5. Continue the lifecycle: tracker update on branch (advance cursor — queue becomes empty, move FMC-6 to Resolved, session-log entry) → commit → **`git status --porcelain` check immediately before `gh pr merge`** (this discipline has caught real bugs before; keep doing it every session) → adversarial review (`git diff dev...HEAD`) → push → PR → merge → **verify `origin/dev`'s log actually contains every commit you made** → prune → re-arm (or wrap the campaign per step 4 above).

## Critical context / traps

- **This repo's campaign default branch is `dev`, not `main`** — same as every prior session; `main` is a separate downstream branch this campaign does not touch unless asked.
- **`gh pr merge --delete-branch` deletes the LOCAL feature branch too**, not just the remote one, when run while that branch is checked out — reconfirmed again this session (`git branch -d feature/FMC-5` would have failed with "branch not found"; verified via `git branch --list` and `git ls-remote --heads origin feature/FMC-5` instead of attempting the now-redundant local delete). Don't be alarmed if step 10's local `git branch -d` errors this way; it means pruning already happened.
- **The `git status --porcelain` check immediately before `gh pr merge` (not just before the last commit) has not recurred as a bug since sessions 3/4** — keep doing it every session regardless of how the session "feels."
- **`gh pr merge --rebase` rewrites commit SHAs.** FMC-5's branch commits `6805b2d`/`6e88fc6`/`467ea37` became `5110ac7`/`4684869`/`78ea64c` on `dev` after the rebase-merge — same content, new hashes. Don't be alarmed when `git log dev` doesn't contain the exact SHAs you committed on the feature branch; verify by content/message instead if you need to confirm a commit landed (`git log --oneline dev | grep <message-fragment>`, not `grep <old-sha>`).
- **FMC-5 (this session) closed out the store.py Notifier work — services/store.py grew by ~130 lines.** Any line numbers cited in FMC-6's task description or in older handovers that reference `store.py` (e.g. the `wait_for_pending_teams_sends` mirror-pattern location) will have shifted. Always re-grep rather than trusting a cited line number for that file specifically.
- FMC-6's 3 sub-issues are independent (different files: permissions.py, messaging.py, presence.py, plus a cross-cutting grep sweep for sub-issue 3) — no shared-class risk like FMC-5 had, safe to fix in any order or even in parallel edits within one pass.
- **Queue is down to 1 item.** Plan for the campaign-complete path (skill's R6) as a real possibility this session, not just FMC-6's own resolution.

## Do not repeat

- Don't batch a `backlog task edit` (or any file mutation) with a `git commit` in the same parallel tool-call round unless you've explicitly staged that exact file in that exact commit — verify with `git status --porcelain` immediately before committing, not just before staging.
- Don't run `backlog task edit --append-notes` (e.g. to record a post-review finding) as an afterthought once the branch is already pushed and the PR is about to be merged — fold that note into the same commit/push cycle as the review itself, before triggering the merge, so there's no window for a dangling uncommitted edit.
- Don't trust a single grep pass when renaming/fixing a string or pattern that recurs in multiple places — re-grep broadly for every instance before considering a fix complete. This bit FMC-5's AC#4 scope question (resolved by checking existing tests rather than guessing) and is explicitly relevant again for FMC-6 AC#3 (every long-poll tool's timeout description, not just the two the task text calls out by name).
- Don't assume a bug-shaped task necessarily needs a code fix without checking exploitability/impact first when the task allows for a no-op or defensive-only outcome — FMC-6's AC#1 may fall into this category (see Next steps item 3): the 3 named tools might not currently be able to raise `ValidationError` at all, making this a contract-consistency fix rather than a live-bug fix. Check before assuming; this is the same "verify-first" caution that applied to FMC-8.
- Don't expand scope silently — if fixing sub-issue 3 (timeout descriptions) surfaces tools beyond what FMC-6's description explicitly lists, that's expected (AC#3 says "each tool", plural, unscoped) and fine to include; but if something ELSE unrelated turns up during the sweep, stop and ask per `backlog instructions task-execution`'s scope-change rule rather than folding it in.
