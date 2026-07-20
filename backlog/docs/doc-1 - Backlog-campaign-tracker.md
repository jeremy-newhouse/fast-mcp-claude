---
id: doc-1
title: Backlog campaign tracker
type: other
created_date: '2026-07-20 20:49'
updated_date: '2026-07-20 20:50'
---
# Backlog campaign tracker

One issue per session. Protocol: restore → take the cursor issue → feature-branch
lifecycle → advance cursor → append session log → write handover.

## Cursor
**Next issue: FMC-7** — queue order confirmed by the user on 2026-07-20 (selected
the proposed "docs first, then by severity within similar risk" order verbatim);
do not re-ask before taking the next item.

## Queue (confirmed order)
| # | Issue | Type | One-line note |
| --- | --- | --- | --- |
| 1 | FMC-7 | docs | Documentation accuracy sweep (CLAUDE.md/README.md drift) — doc-only, no runtime changes |
| 2 | FMC-3 | bug [HIGH] | auth.py process-global self-perpetuating lockout + non-ASCII bearer-token crash |
| 3 | FMC-2 | bug [HIGH] | who() leaks announce_token, defeating the ECA-71 identity guard |
| 4 | FMC-8 | bug [MED] | Verify/fix .mcp.json.example channel server-key mismatch vs channel.py's hardcoded SERVER_NAME |
| 5 | FMC-4 | bug [MED] | Close sandbox path-existence oracle + extend body-size caps to structured fields |
| 6 | FMC-5 | bug [MED] | Fix Notifier/long-poll correctness bugs in services/store.py (4 sub-bugs; riskiest change — touches core long-poll infra) |
| 7 | FMC-6 | bug [LOW] | Fix tool-pattern deviations from CLAUDE.md's documented contract |

## Resolved
| # | Issue | Status/date/session | Evidence summary |
| --- | --- | --- | --- |

## Not queued — needs a human / blocked
(none — all 7 open issues are agent-resolvable with objectively verifiable acceptance criteria)

## Session log
- 2026-07-20 — session 0 (init): Inventoried all 7 open FMC issues via `backlog task list`/`backlog task view`; classified all as agent-resolvable (none need a human at hardware or a product decision). Proposed queue order (docs sweep first for lowest risk/context-building, then High→Medium→Low severity, complexity-ordered within Medium); user confirmed the proposed order verbatim. Created this tracker doc, added `.claude/handovers/` to `.gitignore`, created `archive/handovers/`. Noted pre-existing dirty working tree (modified CLAUDE.md/README.md/fmc-1 task file, untracked `.claude/skills/`, `herdr-tmux-shim/`, and the fmc-2..fmc-8 task files themselves) — left untouched, only the tracker doc + .gitignore change were committed by this session. Wrote first handover for cursor FMC-7.
