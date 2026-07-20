---
id: FMC-7
title: >-
  Documentation accuracy sweep: fix CLAUDE.md/README.md drift from the actual
  implementation
status: To Do
assignee: []
created_date: '2026-07-20 20:26'
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
- [ ] #1 CLAUDE.md no longer contradicts itself on whether the permission relay is implemented, and README.md matches
- [ ] #2 CLAUDE.md module layout lists launcher.py, session.py, and session_hook.py, plus documents presence.forget and the session_relay check operation, and its traversal-defense claim is corrected
- [ ] #3 Root README.md documents worker-supervisor, spawner, sandbox-runner, and start-session.sh, fixes the channel source attribute example, softens the no-central-hub claim, and completes the tool reference table
- [ ] #4 spawner has a README matching its sibling tools, or CLAUDE.md no longer claims every tooling directory has one
- [ ] #5 start-session.sh header comment reflects the actual identity format, the full list of env overrides, the arg-passthrough behavior, and removes dangling doc references
<!-- AC:END -->
