---
id: doc-1
title: Backlog campaign tracker
type: other
created_date: '2026-07-20 20:49'
updated_date: '2026-07-20 21:05'
---
# Backlog campaign tracker

One issue per session. Protocol: restore → take the cursor issue → feature-branch
lifecycle → advance cursor → append session log → write handover.

## Cursor
**Next issue: FMC-3** — queue order confirmed by the user on 2026-07-20 (selected
the proposed "docs first, then by severity within similar risk" order verbatim);
do not re-ask before taking the next item.

## Queue (confirmed order)
| # | Issue | Type | One-line note |
| --- | --- | --- | --- |
| 1 | FMC-3 | bug [HIGH] | auth.py process-global self-perpetuating lockout + non-ASCII bearer-token crash |
| 2 | FMC-2 | bug [HIGH] | who() leaks announce_token, defeating the ECA-71 identity guard |
| 3 | FMC-8 | bug [MED] | Verify/fix .mcp.json.example channel server-key mismatch vs channel.py's hardcoded SERVER_NAME |
| 4 | FMC-4 | bug [MED] | Close sandbox path-existence oracle + extend body-size caps to structured fields |
| 5 | FMC-5 | bug [MED] | Fix Notifier/long-poll correctness bugs in services/store.py (4 sub-bugs; riskiest change — touches core long-poll infra) |
| 6 | FMC-6 | bug [LOW] | Fix tool-pattern deviations from CLAUDE.md's documented contract |

## Resolved
| # | Issue | Status/date/session | Evidence summary |
| --- | --- | --- | --- |
| 1 | FMC-7 | Done, 2026-07-20, session 1 | Doc-only: fixed the permission-relay implemented-vs-not-yet contradiction (CLAUDE.md + README.md, 3 locations); added launcher.py/session.py/session_hook.py to CLAUDE.md module layout + presence.forget + session_relay 'check' op; corrected traversal-defense claim; softened CLAUDE.md's absolute no-central-hub claim; added README Standalone tooling section (worker-supervisor/spawner/sandbox-runner/start-session.sh) + fixed channel source-attribute example + completed the 8-tool teams_outbox/session_relay table rows; created spawner/README.md; rewrote start-session.sh header comment. Verified via grep/read of current source against every claim; `ruff check src/ tests/` and `bash -n start-session.sh` both passed. All 5 ACs checked. |

## Not queued — needs a human / blocked
(none — all 7 open issues are agent-resolvable with objectively verifiable acceptance criteria)

## Session log
- 2026-07-20 — session 0 (init): Inventoried all 7 open FMC issues via `backlog task list`/`backlog task view`; classified all as agent-resolvable (none need a human at hardware or a product decision). Proposed queue order (docs sweep first for lowest risk/context-building, then High→Medium→Low severity, complexity-ordered within Medium); user confirmed the proposed order verbatim. Created this tracker doc, added `.claude/handovers/` to `.gitignore`, created `archive/handovers/`. Noted pre-existing dirty working tree (modified CLAUDE.md/README.md/fmc-1 task file, untracked `.claude/skills/`, `herdr-tmux-shim/`, and the fmc-2..fmc-8 task files themselves) — left untouched, only the tracker doc + .gitignore change were committed by this session. Wrote first handover for cursor FMC-7.
- 2026-07-20 — session 1: Restored from the session-0 handover; ground-truth check found zero drift (HEAD/tracker/FMC-7 status all matched). Resolved FMC-7 on `feature/FMC-7` off `dev` @ `820ab92`: fixed all 6 documentation-drift clusters across CLAUDE.md, README.md, start-session.sh, and a new spawner/README.md. Cursor advances to FMC-3.
