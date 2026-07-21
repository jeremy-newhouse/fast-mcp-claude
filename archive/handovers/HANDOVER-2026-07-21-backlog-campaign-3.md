# Handover — resolve FMC-10 (server auth fails open) — first issue of the fresh 8-issue queue (FMC-9..16)

**Date**: 2026-07-21 | **Grounded against**: `dev` @ `f748957ed2a871f5acfd288d8e02f0bbe773e353`, clean except one unrelated pre-existing untracked dir (see traps below), in sync with `origin/dev` (0 ahead/behind) | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-10 — server.py/__main__.py: MCP auth silently fails open when
MCP_API_KEY is unset/empty (mcp_auth_enabled=true but no key -> auth=None,
server starts anyway, unauthenticated), and __main__.py's startup log
misreports auth_enabled:true in that exact case. Queue order (isolation/
complexity-first: FMC-10, FMC-14, FMC-11, FMC-12, FMC-9, FMC-13, FMC-15,
FMC-16) confirmed by the user on 2026-07-21 — do not re-ask before taking
the next item. Before starting, resolve the untracked
.claude/skills/codex-review/ directory noted below — it will trip the
preflight dirty-tree check.
```

## State
| Item | Status |
| --- | --- |
| Branch | `dev` @ `f748957`, clean except one untracked dir (see traps) |
| Sync with origin | 0 ahead / 0 behind `origin/dev` |
| Leftover `feature/*` branches | none (checked both local and remote) |
| Open PRs | none (`gh` authenticated as jeremy-newhouse, checked) |
| Tracker cursor | FMC-10 (doc-1, just updated and committed this session) |
| FMC-10 task status | To Do, unassigned, Priority High, Type bug |
| Queue after FMC-10 | FMC-14, FMC-11, FMC-12, FMC-9, FMC-13, FMC-15, FMC-16 (7 more after this one) |

## Next steps
1. Preflight per the skill: `git status --porcelain` must be clean. It currently is NOT — see traps below — resolve that first (your call how; it's not part of this campaign's scope).
2. `git checkout -b feature/FMC-10 dev`.
3. Read `backlog instructions task-execution`; `backlog task view FMC-10 --plain` for the full description (self-contained, no need to re-read doc-2); mark In Progress + assign; record your implementation plan on the task.
4. Fix both sub-bugs in `src/fast_mcp_claude/server.py` (~lines 26-35, the `else` branch that only warns when `mcp_auth_enabled=true` but `mcp_api_key` is falsy — should fail startup instead) and `src/fast_mcp_claude/__main__.py` (~line 27, the `auth_enabled` log field that's `is not None` instead of a real truthiness/behavior check).
5. Add tests per FMC-10's 3 acceptance criteria (fail-closed startup behavior, accurate startup log field, both covered by tests that fail pre-fix).
6. Read `backlog instructions task-finalization` before checking any AC — objective command output only.
7. Update the tracker (doc-1) on the branch: move FMC-10 to Resolved, advance cursor to FMC-14, append session-9 log entry.
8. Commit, review (self or adversarial subagent — no PR-approval gate, see the skill's Conventions table), push, open+merge PR into `dev` via `gh pr merge --rebase --delete-branch`, sync local `dev`, delete local branch.
9. Archive this handover to `archive/handovers/` (check for name collisions — `archive/handovers/HANDOVER-2026-07-21-backlog-campaign.md` and `-2.md` already exist from sessions 7 and earlier this same date, so this one needs to become `-3.md` when archived), commit, write the session-9 handover for cursor FMC-14, push `dev`.

## Critical context / traps
- **This campaign's tracker is doc-1, not doc-2.** doc-2 is the Codex full-codebase review report FMC-9..16 were generated from — read it only if a task's own self-contained description isn't enough; don't confuse the two.
- **Unrelated untracked directory will fail your preflight clean-tree check**: `.claude/skills/codex-review/` is untracked, added in an earlier session outside this campaign's scope (it's a codex-review skill, unrelated to any FMC task). It is NOT part of what this campaign should commit under FMC-10's changes. Resolve it however seems right (commit it separately first with its own unrelated commit message, or ask the user) before treating the tree as clean and starting FMC-10's branch — do not silently fold it into FMC-10's commit, and do not delete it.
- **FMC-9 and FMC-13 (later in the queue) are more design-ambiguous than a typical bugfix**: they require inventing an actual trust/provenance mechanism for the channel sidecar's admin/operator authority (there is currently no per-peer identity distinction in this server's single-shared-bearer-key auth model), not just correcting a mechanical logic error. When you reach them, expect to make and document a scoping judgment call, the same way FMC-4 and FMC-8 did.
- **FMC-12 touches the same file (`services/store.py`) FMC-5 already modified** — read FMC-5's resolved-table entry in doc-1 before touching Notifier/cleanup code again, so you don't reintroduce or duplicate that work. FMC-12 is a distinct gap in FMC-5's own growth-fix assumption (inbox:/pubsub: keys), not an overlap.
- **FMC-9/FMC-13 share `channel.py`, and FMC-15/FMC-16 share `launcher.py`** — the queue deliberately sequences each pair back-to-back (5→6, 7→8) specifically to reduce rebase churn; don't reorder them apart without a reason.
- Two related-but-out-of-scope notes surfaced while creating these tasks (recorded in doc-1's session-8 log, not queued, no action needed now): a MEDIUM auth.py finding is actually FMC-3's already-accepted intentional tradeoff, and a MEDIUM store.py finding looks like a subtle regression in FMC-5's own retry-loop rewrite. Neither is part of FMC-10..16's scope.

## Do not repeat
- Don't re-run `/backlog-handover init` — the tracker already exists (doc-1) and reusing it is correct; a second `init` would create a duplicate tracker doc.
- Don't assume the codex review's line numbers are gospel without a quick sanity check against current `dev` — they were verified against `dev @ f748957` and nothing has changed since, but re-verify if `dev` has moved by the time you pick this up.
