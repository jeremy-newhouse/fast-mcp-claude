# herdr-tmux-shim

Claude Code agent-team split panes rendered as native **herdr** panes — no real tmux session, herdr's agent-status sidebar keeps working for every teammate.

> **Scope.** This is opt-in tooling for *interactive* sessions where you're personally using herdr as your terminal multiplexer. It is unrelated to `worker-supervisor`'s automated (headless, pm2-managed) lane spawning, which never uses herdr or `teammateMode: "tmux"` — do not wire this into that path.

## How it works

Claude Code's `teammateMode: "tmux"` doesn't need tmux per se — it shells out to a `tmux` binary with a small, fixed command set (verified by reverse-engineering the `TmuxBackend` in `@anthropic-ai/claude-code` 2.1.215):

`-V`, `has-session`, `new-session`, `new-window`, `list-windows`, `list-panes`, `split-window`, `select-pane -T`, `set-option`, `select-layout`, `resize-pane`, `respawn-pane -k`, `kill-pane`, `kill-session`, `display-message`.

Panes are created running `cat` as a placeholder, then the teammate command is launched via `respawn-pane -k -- <command>`. Pane IDs are treated as opaque strings.

The `tmux` script here impersonates that surface and translates it to herdr's CLI (which Claude Code inherits access to, since it runs inside a herdr pane):

| Claude Code calls | shim does |
| --- | --- |
| `split-window` / `new-session` / `new-window` | `herdr pane split --pane <anchor> --direction ... --no-focus` |
| `select-pane -T <name>` | `herdr pane rename` |
| `respawn-pane -k -- <cmd>` | `herdr pane run <pane> "<cmd>"` |
| `kill-pane` / `kill-session` | `herdr pane close` |
| `send-keys` | `herdr pane send-keys` (named/modifier keys) or `herdr pane send-text` (literal text) |
| layout/border/`set-option` calls | no-op (herdr manages layout itself) |

The first teammate splits off the leader's own pane (`$HERDR_PANE_ID`), later ones tile alongside — so you get the split view in the same herdr tab you're working in. Because each teammate is a real `claude` process in a real herdr pane, herdr's screen-manifest detection gives you blocked/working/idle per teammate in the sidebar — something real tmux mode can't do.

Outside herdr — `HERDR_ENV` isn't `1`, or `HERDR_PANE_ID` isn't set — the shim execs the real tmux, so it's safe to leave on PATH.

## Install (on each machine where you run `claude` interactively via herdr)

This directory lives in the `fast-mcp-claude` repo. Once it's committed and pushed, a `git pull` on any peer machine gets you the files — no zip to re-derive.

1. Make sure both scripts are executable (already true in git, but `chmod` is cheap insurance after a fresh checkout):

   ```sh
   chmod +x herdr-tmux-shim/tmux herdr-tmux-shim/claude-in-herdr
   ```

2. Requirements: `herdr` on PATH, `python3`, and agent teams enabled. The launcher sets `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` and `--teammate-mode tmux` for you.

3. Inside a herdr pane, launch Claude Code via the wrapper instead of plain `claude` (put the dir on PATH, or alias it, or call it by path):

   ```sh
   /path/to/fast-mcp-claude/herdr-tmux-shim/claude-in-herdr
   ```

4. Ask for teammates ("spawn 3 teammates to review this in parallel"). Panes open next to you in herdr.

Debug: `HERDR_TMUX_SHIM_DEBUG=1` logs to `$TMPDIR/herdr-tmux-shim.log`. Override the herdr binary with `HERDR_BIN`.

## Limitations / honest caveats

- **Version coupling.** Built against the tmux call patterns of Claude Code 2.1.215. Agent teams are experimental; if a future version changes its TmuxBackend commands, the shim may need updating (unsupported commands fail loudly with `unsupported command: ...` on stderr — check the debug log).
- **`herdr pane run` types into a shell.** The teammate command is sent as a line to the new pane's shell rather than exec'd directly. Exotic quoting in the teammate command could theoretically break; the commands Claude Code generates are plain single-string commands, which are fine.
- **Layout is herdr's, not tmux's.** `select-layout tiled` / `main-vertical` / `resize-pane -x 30%` are no-ops; herdr's own ratio-based splitting decides geometry. Looks fine in practice, but it's not pixel-identical to the tmux layouts.
- **`remain-on-exit` has no herdr equivalent** — if a teammate process dies, its pane shows a shell prompt instead of tmux's "pane dead" state. Claude Code's own idle/error reporting still works (it goes through the team mailbox, not the pane).
- **Restart edge case**: `respawn-pane -k` on an already-running pane is emulated by close + re-split (the synthetic pane ID stays stable, so Claude Code never notices). Position within the layout may shift.
- Do **not** set `teammateMode: "tmux"` globally in `~/.claude/settings.json` on these hosts if you also use real tmux there without the shim — keep the mode scoped to the wrapper.

## Files

- `tmux` — the shim (Python 3, stdlib only)
- `claude-in-herdr` — launcher that prepends the shim to PATH and enables teams for that one claude process
