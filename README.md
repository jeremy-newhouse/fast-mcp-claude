# fast-mcp-claude

> Peer-to-peer remote control between Claude Code sessions, over HTTP, via MCP.

A FastMCP server that lets two (or more) Claude Code sessions on **different machines** drive and coordinate each other. Each machine runs an instance of this server; each Claude Code session points its `.mcp.json` at its **own** server (for the worker role) and at any **remote** peers it wants to control.

```
Machine A (controller)                       Machine B (worker)
+--------------------------+                 +--------------------------+
| claude (interactive)     |                 | claude (interactive,     |
|  .mcp.json:              |                 |  channel-pushed)         |
|    claude-local  → A     |                 |  .mcp.json:              |
|    claude-peer-b → B     |                 |    claude-local  → B     |
+--------------------------+                 +--------------------------+
            |                                            |
            v                                            v
+--------------------------+   HTTPS+Bearer   +--------------------------+
| fast-mcp-claude @ A      |←---------------→| fast-mcp-claude @ B      |
+--------------------------+                  +--------------------------+
```

The controller calls tools like `send_prompt`, `wait_for_completion`, `approve_tool`, `read_file` — all on the **remote** peer's MCP server. The worker calls `wait_for_instruction`, `reply` — all on its **local** server. Same code, symmetric roles.

## Status

v0.2 — adds Claude Code **Channels** push mode and **N-way** peer presence. Working: peer-to-peer messaging, prompt **push** via a channel adapter (no `/worker` priming, no idle-timeout), identity addressing + presence/roster (`who`), permission relay via PreToolUse hook, file bridge (sandboxed), pub/sub channels, bearer auth with rate-limit lockout. Not yet: permission relay over the native `claude/channel/permission` protocol (needs inbound custom-notification support the Python MCP SDK lacks today — the hook covers approvals meanwhile), end-to-end integration tests against real Claude Code sessions.

## Install

```bash
git clone git@github.com:jeremy-newhouse/fast-mcp-claude.git
cd fast-mcp-claude
uv sync --all-extras
cp .env.example .env
# edit .env — at minimum set PEER_NAME, MCP_API_KEY, PEERS, WORKSPACE_ROOTS
```

For the permission-relay hook to work on a machine, install the package's CLI globally so the `fast-mcp-claude-hook` binary is on PATH:

```bash
uv tool install .
which fast-mcp-claude-hook
```

## Configure two peers

Suppose your machines are `desk` (10.0.0.10) and `laptop` (10.0.0.20). On each, copy `.env.example` to `.env`:

**`desk:.env`**
```bash
PEER_NAME=desk
MCP_HOST=0.0.0.0
MCP_PORT=5473
MCP_API_KEY=<openssl rand -hex 32>          # call this DESK_KEY
PEERS=[{"name":"laptop","url":"http://10.0.0.20:5473/mcp","api_key":"<LAPTOP_KEY>"}]
WORKSPACE_ROOTS=/Users/me/repos
```

**`laptop:.env`**
```bash
PEER_NAME=laptop
MCP_HOST=0.0.0.0
MCP_PORT=5473
MCP_API_KEY=<openssl rand -hex 32>          # call this LAPTOP_KEY
PEERS=[{"name":"desk","url":"http://10.0.0.10:5473/mcp","api_key":"<DESK_KEY>"}]
WORKSPACE_ROOTS=/Users/me/repos
```

> Use Tailscale, a VPN, or Cloudflare Tunnel for connectivity if the machines aren't on the same LAN. Anything that gives each peer a reachable HTTPS URL works.

Start the server on each machine:

```bash
./start.sh                  # pm2
# or for the foreground
uv run fast-mcp-claude
```

## Wire up Claude Code

In **each project** where you'll run Claude Code, create a `.mcp.json`:

```json
{
  "mcpServers": {
    "claude-local": {
      "type": "http",
      "url": "http://localhost:5473/mcp",
      "headers": {
        "Authorization": "Bearer ${MCP_API_KEY}"
      }
    },
    "claude-peer-laptop": {
      "type": "http",
      "url": "http://10.0.0.20:5473/mcp",
      "headers": {
        "Authorization": "Bearer ${PEER_LAPTOP_KEY}"
      }
    }
  }
}
```

(The header values come from your shell environment; e.g. `export PEER_LAPTOP_KEY=<LAPTOP_KEY>` before launching `claude`.)

The same `.mcp.json` works on the laptop side — just rename the peer entry (`claude-peer-desk`) and swap the URL/key.

## Use it

### On the worker side (the machine being controlled)

Launch `claude` in the project, then invoke the bundled slash command:

```
/worker
```

This primes the session with the worker loop (long-poll `wait_for_instruction` → execute → `reply` → loop).

### On the controller side

Launch `claude` in any project that has the same `.mcp.json` peer entry. Then:

```
/control

Please ask the laptop to summarize what's in ~/repos/notes/today.md
```

The session will call `claude-peer-laptop:send_prompt`, then `wait_for_completion`, and surface the result.

## Channels: push mode (recommended)

[Claude Code Channels](https://code.claude.com/docs/en/channels) (research preview, requires Claude Code ≥ v2.1.80) let an MCP server **push** events into a live session instead of the session polling for them. `fast-mcp-claude-channel` is a tiny stdio adapter that bridges your local server's inbox into the running worker session — so a remote controller's `send_prompt` surfaces automatically, with **no `/worker` priming and no MCP idle-timeout**.

Channel mode is **strict opt-in** and arming it takes two switches, by design:

1. Set `CHANNEL_ENABLED=true` in the worker's `.env` (it defaults to `false`).
2. Launch the worker with the dev-channel flag:

```bash
claude --dangerously-load-development-channels server:claude-channel
```

Add the `claude-channel` entry from `.mcp.json.example` and install the CLI (`uv tool install .`) so the adapter binary is on PATH. Prompts then arrive as `<channel source="fast-mcp-claude" message_id="..." sender="...">` events; the worker does the task and calls `reply(message_id, ...)`. Channel pushes are fire-and-forget, so the `reply`/outbox path remains the source of truth for delivery (this is the lesson from claude-peers-mcp's silent-message-loss bugs).

**Coexistence & safety.** Channel mode and the `/worker` long-poll loop read the *same* inbox and coexist with no server change — pick one **per worker launch**, and different peers in a fleet can mix modes freely. The `CHANNEL_ENABLED` switch exists because Claude Code spawns the `claude-channel` adapter whenever it's wired into `.mcp.json`, even when you launched *without* `--dangerously-load-development-channels`. With `CHANNEL_ENABLED=false` (the default) such a wired-but-unintended adapter completes the MCP handshake and then stays **inert** — it never polls, claims, or pushes — so it's safe to leave configured alongside loop mode. Were it to poll while disabled, it would mark inbox messages "delivered" and push them into a channel nobody is listening to: the prompt would vanish and the controller's `wait_for_completion` would hang until TTL. For the same reason, never **double-arm** a single worker — run the channel adapter *or* `/worker` for a given identity, not both.

> Permissions are **not** relayed over the channel yet — Claude Code's native `claude/channel/permission` relay is an inbound custom notification the Python MCP SDK doesn't surface. Keep using the PreToolUse hook (next section) for approvals.

## N-way peer mode (many sessions / many developers)

Controller/worker is only a convention — every server is symmetric, so any number of sessions can address each other. Each session's channel adapter runs with a stable identity (`--identity`, else `CHANNEL_IDENTITY`, else `PEER_NAME`), and that identity doubles as a mailbox:

- **Discover** peers: `who()` → `[{identity, summary, age_seconds}, ...]` — populated by the heartbeated `announce` from each **armed** channel adapter (`CHANNEL_ENABLED=true`). A disabled/inert adapter does not announce, so it won't appear in the roster.
- **Address** a peer: `send_prompt(prompt, recipient_session="<identity>")` routes only to that peer; omit it to let any idle worker take it.

You do **not** need a central hub:

- **Mesh** (small / trusted team): each dev runs their own server; everyone's `.mcp.json` lists the others. Pair with **Tailscale/WireGuard** so every machine has a stable private URL with no public exposure.
- **Hub** (larger team, easy onboarding, team-wide `who`): run **one** instance as the shared server and point everyone's adapter and peer entries at it. Spokes dial out, so NAT "just works," and presence/pub-sub become a team roster/broadcast.

The same binary is mesh node *or* hub — it's a deployment choice, not a code change. Start mesh-over-Tailscale; adopt a hub later by repointing adapters, with no rewrite.

## Permission relay (optional)

If you want the controller to approve/deny tool calls on the worker, install the `fast-mcp-claude-hook` (see Install above) and add a `PreToolUse` hook to the **worker** project's `.claude/settings.json`. Template is in `.claude/settings.example.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write|NotebookEdit",
        "hooks": [{"type": "command", "command": "fast-mcp-claude-hook"}]
      }
    ]
  }
}
```

On the controller side, `pending_approvals` / `wait_for_pending_approval` surface requests and `approve_tool` decides them.

If the controller doesn't respond within `CRM_DECISION_TIMEOUT` (default 300s), the hook falls back to `permissionDecision: "ask"` so Claude Code's normal permission UI takes over.

## Tool reference

| Tool | Caller | Description |
|---|---|---|
| `send_prompt(prompt, sender?, recipient_session?, metadata?)` | Controller | Queue a prompt on the remote peer |
| `wait_for_completion(message_id, timeout?)` | Controller | Long-poll for the worker's reply |
| `get_status()` | Either | Peer name, version, queue depths |
| `interrupt(session_id?)` | Controller | Signal worker to stop current turn |
| `cancel(message_id)` | Controller | Cancel a queued/in-flight message |
| `list_messages(status?, limit?)` | Either | Observability |
| `wait_for_instruction(recipient_session?, timeout?)` | Worker | Long-poll local inbox |
| `reply(message_id, response)` | Worker | Post completion |
| `consume_interrupt(session_id?)` | Worker | Check/clear interrupt flag |
| `request_approval(...)` | Hook only | Create pending permission |
| `await_decision(approval_id, timeout?)` | Hook only | Block until decided |
| `pending_approvals(limit?)` | Controller | List pending |
| `wait_for_pending_approval(timeout?)` | Controller | Long-poll for new pending |
| `approve_tool(approval_id, decision, reason?)` | Controller | Decide |
| `list_files(path, include_hidden?)` | Controller | Directory listing in workspace |
| `read_file(path)` | Controller | Read text file |
| `write_file(path, content, overwrite?)` | Controller | Write text file |
| `publish(channel, payload, sender?)` | Either | Broadcast on a channel |
| `subscribe(channel, after_id, timeout?)` | Either | Long-poll for new channel messages |
| `announce(identity, summary?, metadata?)` | Any | Heartbeat presence (channel adapter does this) |
| `who(stale_seconds?)` | Any | List peers present on this server |

All tools return `{"success": bool, ...}` or `{"success": false, "error": {"message": ..., "code": ...}}`.

## Security

- **Always set `MCP_API_KEY`** for any non-localhost deployment. The server logs a `WARNING` on startup if it's unset.
- **WORKSPACE_ROOTS is an allowlist**: `read_file`/`write_file` refuse paths outside it, including via symlink escapes.
- **No outbound HTTP from user input**: tools never make network calls to URLs supplied by callers (in v1 there's no outbound HTTP at all).
- **Hook fail-safe**: any error in the permission relay (server down, timeout, parse error) → `permissionDecision: "ask"` → Claude Code's local prompt takes over.
- **Body-size caps** (see `utils/validation.py`): prompt ≤1MB, response ≤4MB, file ≤10MB, pubsub payload ≤256KB.

## Interactive tooling: herdr-tmux-shim (optional)

[`herdr-tmux-shim/`](herdr-tmux-shim/) is a standalone, **opt-in** shim for developers who run Claude Code interactively inside herdr (a personal terminal-pane manager) panes: it impersonates the `tmux` binary Claude Code's experimental agent-teams (`teammateMode: "tmux"`) shells out to, so teammate split panes open as native herdr panes (with herdr's idle/working/blocked sidebar) instead of a real tmux session. It has nothing to do with the MCP server or `worker-supervisor`'s headless pm2 lane spawning — see [`herdr-tmux-shim/README.md`](herdr-tmux-shim/README.md) for how it works and install steps.

## Architectural notes

See [CLAUDE.md](CLAUDE.md) for the deep-dive on module layout, the long-poll notifier pattern, and the permission-relay protocol. Highlights:

- **In-process notifier only**: each peer machine has one server process, so cross-process notification is unnecessary.
- **Push or poll**: the HTTP server is long-poll (the `/worker` loop calls `wait_for_instruction`), but `fast-mcp-claude-channel` adds true push — it long-polls the server out-of-band and emits `notifications/claude/channel` into the live session, so the model never blocks on a tool call to receive work.
- **MCP idle timeout**: `POLL_MAX_WAIT_S` defaults to 25s to stay below Claude Code's MCP idle limit on the long-poll path. Channel push sidesteps the limit entirely (the wait happens in the adapter process, not a Claude tool call).
- **Channels (research preview)**: implemented for prompt delivery (see [Channels: push mode](#channels-push-mode-recommended)). The design mirrors Anthropic's [Channels](https://code.claude.com/docs/en/channels) — the channel adapter is exactly the "thin facade" this section once anticipated. Permission relay over the native `claude/channel/permission` protocol remains future work pending inbound custom-notification support in the Python MCP SDK; the PreToolUse hook covers approvals today.

## Reference architecture

Patterns (auth, logging, config, error envelope, start.sh, pyproject layout) are copied from [`fast-mcp-jira`](https://github.com/jeremy-newhouse/fast-mcp-jira) — the canonical FastMCP reference in this family.

## License

Private — not yet licensed for redistribution.
