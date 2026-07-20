---
id: FMC-7
title: >-
  Documentation accuracy sweep: fix CLAUDE.md/README.md drift from the actual
  implementation
status: Done
assignee:
  - '@claude'
created_date: '2026-07-20 20:26'
updated_date: '2026-07-20 21:09'
labels:
  - documentation
dependencies: []
priority: low
type: docs
ordinal: 7000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Two independent agent-team dogfooding reviews (2026-07-20) — one of src/fast_mcp_claude/ against CLAUDE.md, one of the repo's documentation and tooling directories against their own claims — converged on the same class of problem: README.md/CLAUDE.md now lag the actual implementation by roughly one feature generation, including one outright self-contradiction. Six clusters of fixes, all doc/comment-only (no runtime behavior changes):

1. Permission-relay contradiction (most serious). CLAUDE.md:119 says "Permission relay (now works in Python)" with a full implementation described; CLAUDE.md's own Known Limitations section (:136) still says "not yet implemented (inbound custom-notification gap in the Python MCP SDK)". README.md:26, :149, and :233 all still carry the stale not-implemented claim. Reality sides with :119 — start-session.sh:20-22,220-222 depends on the relay and forces --permission-mode default specifically so it can gate. Pick the true current state and make all four locations agree.

2. CLAUDE.md module-layout gaps. launcher.py (1551 lines — the module that spawns `claude -p` subprocesses under a cwd allowlist and tools ceiling, the largest and most security-sensitive module in the repo), session.py (405 lines, referenced four times in prose without ever being introduced), and session_hook.py (82 lines) all exist with console-script entry points in pyproject.toml but are absent from CLAUDE.md's Module layout section (~lines 38-54). presence.forget (presence.py:84) is also undocumented there and missing from the server's own `instructions` string (server.py:85). session_relay._VALID_OPS includes "check" (session_relay.py:39) but every tool description only mentions 'list' or 'send'. CLAUDE.md:126 claims "traversal patterns blocked at input" but the code has no literal ".." check — it relies on resolve(), which is the better defense but not what's documented.

3. Root README.md tooling coverage. The root README documents only herdr-tmux-shim/ among the repo's standalone tooling directories — worker-supervisor/, spawner/, sandbox-runner/, and start-session.sh are never mentioned there at all. Separately, README.md:145 shows the channel notification's source attribute as `<channel source="fast-mcp-claude" ...>` when the actual value is `fast-mcp-claude-channel` (channel.py:120, matching CLAUDE.md:116). CLAUDE.md:9 ("There is no central hub") contradicts store.py's hub-drain comments and README.md's own documented hub deployment mode (~line 161) — should say no hub is *required*, not that none exists. README.md's Tool reference table (~lines 188-211) omits all eight teams_outbox.py/session_relay.py tools that CLAUDE.md documents as shipped (request_teams_send, await_teams_send, wait_for_pending_teams_send, complete_teams_send, request_session_op, await_session_op, wait_for_pending_session_ops, complete_session_op).

4. spawner/ has no README.md — only a `description =` string in its pyproject.toml — while CLAUDE.md:11 claims every top-level tooling directory has "its own README". worker-supervisor/, sandbox-runner/, and herdr-tmux-shim/ all do. sandbox-runner/README.md calls the spawner "the only launcher" owning run flags, secret mounts, and egress config, making the least self-documented component the most security-relevant one.

5. start-session.sh header comment drift (lines ~1-28). The identity-format example says `<peer>.<repo>`; the actual format (implemented at lines 87-100, per ADR-0016) is `<peer>.<repo>.<name-slug>`, falling back to `<peer>.<repo>-<hash>` on detached HEAD — this is the address operators type into `/fleet-inbox <identity>`, so an accurate example matters. The documented env-override list (line 28: PEER_NAME, MCP_API_KEY, MCP_LOCAL_URL, FLEET_IDENTITY, CHANNEL_MODE) omits SESSION_NAME and SESSION_DESCRIPTION (ECA-23) and MCP_PORT, all of which the script actually reads — SESSION_NAME in particular is the documented way to run two sessions in the same repo, so omitting it hides a real feature. The header never mentions that unrecognized CLI args pass straight through to the final `exec claude ... "$@"` (confirmed true in both code branches). Line 23 points to a non-existent `docs/channels` directory (no docs/ dir exists in this repo) and cites ADR-0010 for the channels design while CLAUDE.md:112 cites ADR-0012 for the same feature — pick one and fix the dangling reference.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 CLAUDE.md no longer contradicts itself on whether the permission relay is implemented, and README.md matches
- [x] #2 CLAUDE.md module layout lists launcher.py, session.py, and session_hook.py, plus documents presence.forget and the session_relay check operation, and its traversal-defense claim is corrected
- [x] #3 Root README.md documents worker-supervisor, spawner, sandbox-runner, and start-session.sh, fixes the channel source attribute example, softens the no-central-hub claim, and completes the tool reference table
- [x] #4 spawner has a README matching its sibling tools, or CLAUDE.md no longer claims every tooling directory has one
- [x] #5 start-session.sh header comment reflects the actual identity format, the full list of env overrides, the arg-passthrough behavior, and removes dangling doc references
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. CLAUDE.md: (a) soften line 9 'no central hub' -> 'no hub is required'; (b) add launcher.py, session.py, session_hook.py to Module layout (~38-54) with accurate one-line descriptions from their own module docstrings; add presence.forget to the presence.py bullet; mention session_relay's 'check' op on its bullet; (c) fix line 126 traversal-defense claim (validation.py has no literal '..' check, relies on resolve()+relative_to()); (d) resolve the permission-relay contradiction by rewriting Known Limitations (~136) to match the implemented-via-stdio-tee reality already described at ~119, noting it's a workaround (not the SDK's typed API) so it could break if Claude Code's stdio framing changes.
2. README.md: sync Status (~26), the Channels-section blockquote (~149), and Architectural notes highlights (~233) to the same implemented-relay reality; fix the channel source attribute example (~145: fast-mcp-claude -> fast-mcp-claude-channel); add a Standalone tooling section covering worker-supervisor/, spawner/, sandbox-runner/, start-session.sh (one-liners sourced from each dir's own README/pyproject description); complete the Tool reference table (~188-211) with the 8 teams_outbox.py/session_relay.py tools (request_teams_send, await_teams_send, wait_for_pending_teams_send, complete_teams_send, request_session_op, await_session_op, wait_for_pending_session_ops, complete_session_op).
3. Create spawner/README.md (mirrors worker-supervisor/README.md and sandbox-runner/README.md style, sourced from spawner/pyproject.toml's description + module docstrings) so CLAUDE.md:11's 'every tooling dir has its own README' claim stays true (AC #4), rather than weakening the CLAUDE.md claim.
4. start-session.sh header comment (lines 1-28): fix the identity-format example to <peer>.<repo>.<name-slug> (falling back to <peer>.<repo>-<hash> on detached HEAD, per the actual logic at ~72-100); add SESSION_NAME, SESSION_DESCRIPTION, MCP_PORT to the env-override list; note that unrecognized CLI args pass through to the final exec claude ... "$@"; fix the dangling docs/channels reference (no docs/ dir exists in this repo) and reconcile ADR-0010 vs CLAUDE.md's ADR-0012 citation for the same channels feature.
All changes are doc/comment-only; no src/*.py runtime logic changes. Verify each AC per task-finalization by re-reading the actual current file content at each location (line numbers have drifted since the review that filed this task).
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Verified all 6 clusters against CURRENT repo state (not the stale line numbers in the task description, which predate 4d28753/e9cc399/820ab92):
- Permission relay: confirmed implemented via stdio-tee (channel.py, matches CLAUDE.md:122). Rewrote CLAUDE.md's Known Limitations (~136), README.md Status (~26), the Channels-section blockquote (~149), and Architectural notes (~248) to agree: implemented via a raw-stdio tee workaround (not the SDK's typed API), PreToolUse hook remains for headless launcher workers only.
- Module layout: confirmed via `wc -l` + pyproject.toml [project.scripts] that launcher.py(1551)/session.py(405)/session_hook.py(82) exist with console entry points but were absent from CLAUDE.md; added all three plus presence.forget (grep-verified in tools/presence.py) and session_relay's 'check' op (grep-verified in _VALID_OPS, tools/session_relay.py:39).
- Traversal claim: read utils/validation.py:125-165 — confirmed no literal '..' check, only Path.resolve(strict=False)+relative_to(); corrected CLAUDE.md:129.
- Central-hub claim: CLAUDE.md:9 softened to 'no hub required'; confirmed README's own hub section (~158-163) was already consistent, no change needed there.
- Root README tooling coverage: added a Standalone tooling section covering worker-supervisor/, spawner/, sandbox-runner/, start-session.sh (one-liners sourced from each dir's own README/pyproject description) alongside the existing herdr-tmux-shim entry; fixed the channel source attribute example (fast-mcp-claude -> fast-mcp-claude-channel, grep-verified against channel.py:99-120); completed the Tool reference table with all 8 teams_outbox.py/session_relay.py tools (grep-verified signatures/descriptions against source).
- spawner/: confirmed no README.md exists (only pyproject.toml description) via `ls`; created spawner/README.md mirroring worker-supervisor/README.md and sandbox-runner/README.md's structure, sourced from spawner/pyproject.toml's description plus each module's own docstring (app.py, config.py, consumer.py, processor.py, launcher.py, relay.py, presence.py, store.py, bus_contract.py) so CLAUDE.md:11's 'every tooling dir has its own README' claim stays true.
- start-session.sh header: rewrote lines 1-32 — identity example corrected to <peer>.<repo>.<name-slug> with the <peer>.<repo>-<hash> detached-HEAD fallback (verified against the actual logic at lines 72-100); added SESSION_NAME/SESSION_DESCRIPTION/MCP_PORT to the env-override list (all three grep-verified as read by the script); added the arg-passthrough note (both exec branches end in "$@"); replaced the dangling docs/channels reference (confirmed no docs/ dir exists in this repo) and reconciled ADR-0010 -> ADR-0012 to match CLAUDE.md's own citation for the same feature.

Verification: `uv run ruff check src/ tests/` -> all checks passed (no src/*.py touched). `bash -n start-session.sh` -> syntax OK. Every AC re-checked by grepping/reading the actual current file content at each claim, not by trusting the task description's line numbers.

Adversarial branch review (independent subagent, dev...feature/FMC-7 diff): confirmed doc-only scope (zero src/*.py touched) and verified every new factual claim against current source. One cosmetic finding: spawner/README.md's Run & test section named the fake test classes as _FakeJS/_FakeMsg (lifted from a comment describing a DIFFERENT codebase's tests in test_integration_nats.py) instead of spawner's own FakeMsg/FakeJs/FakeProcessor/etc. (test_consumer.py). Fixed and committed.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed all 6 clusters of CLAUDE.md/README.md drift (doc/comment-only, no src/*.py changes): resolved the permission-relay implemented-vs-not-yet-implemented contradiction across CLAUDE.md and README.md (3 locations) in favor of the implemented-via-stdio-tee reality; added launcher.py/session.py/session_hook.py to CLAUDE.md's module layout plus presence.forget and session_relay's check op; corrected the traversal-defense claim to canonicalization (not literal '..' matching); softened CLAUDE.md's absolute no-central-hub claim; added a Standalone tooling section to README.md covering worker-supervisor/spawner/sandbox-runner/start-session.sh, fixed the channel source-attribute example, and completed the tool reference table with the 8 teams_outbox/session_relay tools; created spawner/README.md so every tooling dir keeps its own README; rewrote start-session.sh's header comment (identity format, full env-override list, arg passthrough, dangling docs/channels+ADR reference). Verified by grep/read of current source against every claim (ruff check src/ tests/ passed; bash -n start-session.sh passed).
<!-- SECTION:FINAL_SUMMARY:END -->
