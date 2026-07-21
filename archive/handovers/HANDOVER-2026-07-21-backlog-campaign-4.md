# Handover — resolve FMC-14 (hook.py Client(headers=...) incompatible with fastmcp 3.4.4) — second issue of the 8-issue queue (FMC-9..16)

**Date**: 2026-07-21 | **Grounded against**: `dev` @ `fae2ec369065784123e4e23ce879384b21ddfa26`, clean, 1 commit ahead of `origin/dev` (this session's own archive-handover commit, about to be pushed) | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-14 — hook.py's authenticated relay path constructs its fastmcp
Client with a `headers` kwarg the installed fastmcp 3.4.4 Client constructor
does not accept, raising TypeError before every tool call in any
authenticated deployment (MCP_API_KEY set) -- caught by main()'s fallback,
so it silently degrades to Claude Code's local permission UI instead of
ever reaching request_approval/await_decision. Queue order (isolation/
complexity-first: FMC-10 [done], FMC-14, FMC-11, FMC-12, FMC-9, FMC-13,
FMC-15, FMC-16) confirmed by the user on 2026-07-21 — do not re-ask before
taking the next item.
```

## State
| Item | Status |
| --- | --- |
| Branch | `dev` @ `fae2ec3`, clean |
| Sync with origin | 1 ahead / 0 behind `origin/dev` (this session's archive-handover commit not yet pushed — push it as part of restore's preflight/step-0 sync, or it'll happen automatically if you push before starting FMC-14's branch) |
| Leftover `feature/*` branches | none (checked local + remote, pruned) |
| Open PRs | none (`gh pr list --state open` empty) |
| Tracker cursor | FMC-14 (doc-1, updated and committed this session) |
| FMC-14 task status | To Do, unassigned, Priority High, Type bug |
| Queue after FMC-14 | FMC-11, FMC-12, FMC-9, FMC-13, FMC-15, FMC-16 (6 more after this one) |

## Next steps
1. Preflight per the skill: confirm `git status --porcelain` is clean (should be) and push the 1 ahead commit to `origin/dev` if not already done.
2. `git checkout -b feature/FMC-14 dev`.
3. Read `backlog instructions task-execution`; `backlog task view FMC-14 --plain` for the full self-contained description; mark In Progress + assign; record implementation plan.
4. **Before writing any fix**, verify the installed fastmcp 3.4.4 `Client` constructor's actual auth-supplying parameter/mechanism against the real installed package (`uv run python -c "from fastmcp import Client; help(Client.__init__)"` or read the installed source under `.venv`) — do NOT assume based on older fastmcp docs or training-data memory; the task description explicitly warns this exact kind of assumption caused the original bug. Context7 MCP may have current fastmcp docs if useful, but installed-package ground truth wins if they conflict.
5. Fix `hook.py`'s `_relay()` (~lines 153-157) to construct the `Client` using whatever the installed version actually supports (likely an `auth=` parameter or an `httpx.Auth`-compatible object, per the task's own fix-direction note — confirm before committing to an approach).
6. Add a regression test (AC#3) that exercises the authenticated relay path end-to-end against the actual installed fastmcp version — Client construction + at least one real call (e.g. `request_approval`) against a running local server — so a future incompatible-construction regression fails the test suite instead of silently degrading to the `ask` fallback. Look at how `tests/test_channel.py`/`tests/test_launcher.py` spin up a local server/client pair, if they do, for a pattern to follow.
7. Verify AC#1 and AC#2 with objective evidence: an authenticated deployment (`MCP_API_KEY` set) completing a full controller-approval round trip (`request_approval` → `approve_tool` (simulated controller decision) → `await_decision` → hook emits `allow`/`deny`, not falling back to `ask`).
8. Read `backlog instructions task-finalization` before checking any AC.
9. Update the tracker (doc-1) on the branch: move FMC-14 to Resolved, advance cursor to FMC-11, append session-10 log entry.
10. Commit, review (self or adversarial subagent — the last session's adversarial-subagent pass on FMC-10 caught 2 real minor issues self-review missed, so lean toward using one again for anything touching security-relevant paths like this), push, open+merge PR into `dev` via `gh pr merge --rebase --delete-branch`, sync local `dev`, delete local branch.
11. Archive this handover to `archive/handovers/` (check for name collisions — `archive/handovers/HANDOVER-2026-07-21-backlog-campaign.md`, `-2.md`, and `-3.md` already exist from earlier today, so this one becomes `-4.md`), commit, write the session-10 handover for cursor FMC-11, push `dev`.

## Critical context / traps
- **This campaign's tracker is doc-1, not doc-2.** doc-2 is the Codex full-codebase review report FMC-9..16 were generated from — read it only if FMC-14's own self-contained description isn't enough.
- **Don't trust remembered/training-data fastmcp API shape.** The whole bug being fixed here is "assumed a kwarg the installed version doesn't have." Check the actually-installed fastmcp 3.4.4 `Client` API directly before writing the fix (see Next step 4).
- **FMC-5's hook.py note is unrelated**: FMC-5's description mentions a different hook.py defect (the `elapsed += chunk` timeout-accounting loop around what's now line 192 in `_relay()`'s retry loop) that FMC-5 explicitly did NOT touch. FMC-14 is a distinct bug (Client construction) in the same file — don't conflate the two or assume FMC-5 already covered this.
- **FMC-9 and FMC-13 (later in the queue) are more design-ambiguous than a typical bugfix**: they require inventing an actual trust/provenance mechanism for the channel sidecar's admin/operator authority. Expect a documented scoping judgment call when you reach them, same as FMC-4/FMC-8/FMC-10 already made.
- **FMC-12 touches `services/store.py`**, which FMC-5 already modified — read FMC-5's resolved-table entry in doc-1 before touching Notifier/cleanup code again.
- **FMC-9/FMC-13 share `channel.py`, and FMC-15/FMC-16 share `launcher.py`** — the queue deliberately sequences each pair back-to-back to reduce rebase churn.
- Two related-but-out-of-scope notes from session 8 (doc-1's session log, not queued): a MEDIUM auth.py finding is FMC-3's already-accepted intentional tradeoff, and a MEDIUM store.py finding looks like a subtle regression in FMC-5's own retry-loop rewrite. Neither is part of FMC-9..16's scope.
- **From FMC-10's review (session 9)**: an adversarial subagent review pass caught 2 real minor issues a self-review missed (a "single source of truth" property that wasn't actually being used as one, and a stale `.env.example` comment) — worth repeating that pattern for security-relevant fixes like this one.

## Do not repeat
- Don't re-run `/backlog-handover init` when a tracker/queue/cursor/handover already exist and are current — session 9 confirmed with the user that `restore` was the correct mode in that situation; a second `init` would just duplicate the tracker. (This is now the established norm for this campaign, not just a one-off.)
- Don't assume fastmcp's `Client` constructor API from memory or older docs — verify against the installed 3.4.4 package directly (see Next step 4). This is the specific mistake that created the bug FMC-14 fixes.
