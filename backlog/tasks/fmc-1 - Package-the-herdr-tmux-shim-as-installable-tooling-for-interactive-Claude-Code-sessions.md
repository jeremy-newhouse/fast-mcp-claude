---
id: FMC-1
title: >-
  Package the herdr-tmux-shim as installable tooling for interactive Claude Code
  sessions
status: To Do
assignee: []
created_date: '2026-07-19 17:36'
labels:
  - tooling
  - dev-experience
dependencies: []
priority: low
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The operator has a working prototype (currently ~/herdr.zip on their dev machine, reviewed 2026-07-19) that lets Claude Code's experimental agent-teams feature (teammateMode: "tmux", split-pane sub-agents) render as native herdr panes instead of needing a real tmux session, when Claude Code runs inside a herdr-managed terminal pane. herdr is the operators personal terminal-pane manager with its own agent-status sidebar (idle/working/blocked detection per pane) - something real tmux mode does not give Claude Code teams natively.

Mechanism (verified working, reverse-engineered against the TmuxBackend in @anthropic-ai/claude-code 2.1.215): a small stdlib-only Python script named `tmux` intercepts the exact fixed set of tmux CLI calls Claude Code's TmuxBackend issues (-V, has-session, new-session, new-window, list-windows, list-panes, split-window, select-pane -T, set-option, select-layout, resize-pane, respawn-pane -k, kill-pane, kill-session, display-message) and translates them to `herdr pane ...` commands, persisting a small synthetic pane-id mapping to a per-socket JSON state file. A `claude-in-herdr` shell launcher prepends the shim to PATH and sets CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 --teammate-mode tmux for that one claude invocation. Outside a herdr pane (HERDR_ENV != 1) the shim transparently execs the real tmux, so it is safe to leave on PATH permanently.

This belongs in fast-mcp-claude, not evolv-coder-agent (eCA) - eCA is the Teams/Hermes orchestration brain and has no involvement in how an individual Claude Code CLI process renders its terminal UI; fast-mcp-claude (start-session.sh, worker-supervisor, fast-mcp-claude-launcher) is what actually spawns and manages Claude Code processes on the peer machines (mbpm2, mbam5, mini2) where this would be used interactively.

Scope note: this is opt-in tooling for INTERACTIVE sessions where the operator is personally using herdr as their terminal multiplexer. It is explicitly NOT about worker-supervisor's automated lane spawning, which is headless/pm2-managed and does not use herdr or tmux teammate-mode.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The shim (tmux script + claude-in-herdr launcher + README) lands in the fast-mcp-claude repo at a location decided during implementation, both scripts executable
- [ ] #2 Repo documentation (README.md and/or CLAUDE.md) references the shims existence, purpose, and install steps so a future session or the operator on a new machine can find and use it without re-deriving it from a zip file
- [ ] #3 Live-verified on at least one peer machine where the operator runs Claude Code interactively via herdr: spawning an agent-team teammate opens a native herdr pane (not a real tmux session) with herdr's status sidebar correctly reflecting that teammate's idle/working/blocked state
- [ ] #4 Scope explicitly confirmed as opt-in/interactive-only tooling, NOT wired into worker-supervisor's automated (headless, pm2-managed) lane spawning
<!-- AC:END -->
