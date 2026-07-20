---
id: doc-1
title: Backlog campaign tracker
type: other
created_date: '2026-07-20 20:49'
updated_date: '2026-07-20 22:07'
---
# Backlog campaign tracker

One issue per session. Protocol: restore → take the cursor issue → feature-branch
lifecycle → advance cursor → append session log → write handover.

## Cursor
**Next issue: FMC-8** — queue order confirmed by the user on 2026-07-20 (selected
the proposed "docs first, then by severity within similar risk" order verbatim);
do not re-ask before taking the next item.

## Queue (confirmed order)
| # | Issue | Type | One-line note |
| --- | --- | --- | --- |
| 1 | FMC-8 | bug [MED] | Verify/fix .mcp.json.example channel server-key mismatch vs channel.py's hardcoded SERVER_NAME |
| 2 | FMC-4 | bug [MED] | Close sandbox path-existence oracle + extend body-size caps to structured fields |
| 3 | FMC-5 | bug [MED] | Fix Notifier/long-poll correctness bugs in services/store.py (4 sub-bugs; riskiest change — touches core long-poll infra) |
| 4 | FMC-6 | bug [LOW] | Fix tool-pattern deviations from CLAUDE.md's documented contract |

## Resolved
| # | Issue | Status/date/session | Evidence summary |
| --- | --- | --- | --- |
| 1 | FMC-7 | Done, 2026-07-20, session 1 | Doc-only: fixed the permission-relay implemented-vs-not-yet contradiction (CLAUDE.md + README.md, 3 locations); added launcher.py/session.py/session_hook.py to CLAUDE.md module layout + presence.forget + session_relay 'check' op; corrected traversal-defense claim; softened CLAUDE.md's absolute no-central-hub claim; added README Standalone tooling section (worker-supervisor/spawner/sandbox-runner/start-session.sh) + fixed channel source-attribute example + completed the 8-tool teams_outbox/session_relay table rows; created spawner/README.md; rewrote start-session.sh header comment. Verified via grep/read of current source against every claim; `ruff check src/ tests/` and `bash -n start-session.sh` both passed. All 5 ACs checked. |
| 2 | FMC-3 | Done, 2026-07-20, session 2 | Fixed 2 bugs in src/fast_mcp_claude/auth.py::ApiKeyVerifier.verify_token: (1) process-global self-perpetuating lockout — reordered so a correct token is compared and accepted BEFORE consulting the rate limiter, so it always succeeds even during an active lockout (verified verify_token receives no per-connection identity to scope the limiter to just the attacker); (2) non-ASCII bearer crash — hmac.compare_digest now runs on UTF-8-encoded bytes instead of raw str, so a non-ASCII token yields a clean None/401 instead of an uncaught TypeError, and is correctly counted as a rate-limiter failure. Added 3 tests to tests/test_auth.py covering both scenarios. Verified: `uv run pytest tests/test_auth.py -v` (10 passed), full `uv run pytest` (251 passed), `uv run ruff check src/ tests/` (clean). All 3 ACs checked. |
| 3 | FMC-2 | Done, 2026-07-20, session 3 | Fixed the announce_token leak in who() (src/fast_mcp_claude/tools/presence.py): redact credential-shaped metadata keys (reusing logging_config.py's existing SENSITIVE_LOG_FIELDS_EXACT/SENSITIVE_LOG_SUFFIXES blacklist) from each peer's metadata at the who() tool boundary, not in the shared store.list_presence()/_row_to_presence() layer (existing tests rely on that layer still exposing the raw token to verify the ECA-71 guard end-to-end). forget()/announce()'s owner-token guard needed no change — it reads the token via its own raw SQL query, never through the redacted path. Added 2 tests to tests/test_presence.py. Verified: `uv run pytest tests/test_presence.py -v` (15 passed, all pre-existing tests unmodified and passing), full `uv run pytest` (253 passed), `uv run ruff check src/ tests/` (clean). All 3 ACs checked. |

## Not queued — needs a human / blocked
(none — all 7 open issues are agent-resolvable with objectively verifiable acceptance criteria)

## Session log
- 2026-07-20 — session 0 (init): Inventoried all 7 open FMC issues via `backlog task list`/`backlog task view`; classified all as agent-resolvable (none need a human at hardware or a product decision). Proposed queue order (docs sweep first for lowest risk/context-building, then High→Medium→Low severity, complexity-ordered within Medium); user confirmed the proposed order verbatim. Created this tracker doc, added `.claude/handovers/` to `.gitignore`, created `archive/handovers/`. Noted pre-existing dirty working tree (modified CLAUDE.md/README.md/fmc-1 task file, untracked `.claude/skills/`, `herdr-tmux-shim/`, and the fmc-2..fmc-8 task files themselves) — left untouched, only the tracker doc + .gitignore change were committed by this session. Wrote first handover for cursor FMC-7.
- 2026-07-20 — session 1: Restored from the session-0 handover; ground-truth check found zero drift (HEAD/tracker/FMC-7 status all matched). Resolved FMC-7 on `feature/FMC-7` off `dev` @ `820ab92`: fixed all 6 documentation-drift clusters across CLAUDE.md, README.md, start-session.sh, and a new spawner/README.md. Cursor advances to FMC-3.
- 2026-07-20 — session 2: Restored from the session-1 handover; ground-truth check found zero drift (HEAD/tracker/FMC-3 status all matched, no leftover branches/PRs). Resolved FMC-3 on `feature/FMC-3` off `dev` @ `0c5cfeb`: fixed the process-global self-perpetuating lockout and the non-ASCII bearer-token crash in auth.py, added 3 tests. Cursor advances to FMC-2.
- 2026-07-20 — session 3: Restored from the session-2 handover; ground-truth check found zero drift (HEAD/tracker/FMC-2 status all matched, no leftover branches/PRs, working tree clean and in sync with origin/dev @ dae8924). Resolved FMC-2 on `feature/FMC-2` off `dev` @ `dae8924`: redacted announce_token (and other credential-shaped metadata keys) from who()'s output at the tool boundary in presence.py, reusing logging_config.py's existing sensitive-field blacklist; confirmed the forget/announce owner-token guard is unaffected since it reads the token via its own raw store query; added 2 regression tests. Cursor advances to FMC-8.
