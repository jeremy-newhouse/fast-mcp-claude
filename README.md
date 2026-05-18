# fast-mcp-claude

> Peer-to-peer remote control between Claude Code sessions, over HTTP, via MCP.

A FastMCP server that lets two (or more) Claude Code sessions on **different machines** drive and coordinate each other. Each machine runs an instance of this server; each Claude Code session points its `.mcp.json` at its **own** server (for the worker role) and at any **remote** peers it wants to control.

```
Machine A (controller)                       Machine B (worker)
+--------------------------+                 +--------------------------+
| claude (interactive)     |                 | claude (interactive,     |
|  .mcp.json:              |                 |  primed by /worker)      |
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

v0.1 — initial usable cut. Working: peer-to-peer messaging, permission relay via PreToolUse hook, file bridge (sandboxed), pub/sub channels, bearer auth with rate-limit lockout. Not yet: Channels-plugin facade, end-to-end integration tests against real Claude Code sessions.

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

All tools return `{"success": bool, ...}` or `{"success": false, "error": {"message": ..., "code": ...}}`.

## Security

- **Always set `MCP_API_KEY`** for any non-localhost deployment. The server logs a `WARNING` on startup if it's unset.
- **WORKSPACE_ROOTS is an allowlist**: `read_file`/`write_file` refuse paths outside it, including via symlink escapes.
- **No outbound HTTP from user input**: tools never make network calls to URLs supplied by callers (in v1 there's no outbound HTTP at all).
- **Hook fail-safe**: any error in the permission relay (server down, timeout, parse error) → `permissionDecision: "ask"` → Claude Code's local prompt takes over.
- **Body-size caps** (see `utils/validation.py`): prompt ≤1MB, response ≤4MB, file ≤10MB, pubsub payload ≤256KB.

## Architectural notes

See [CLAUDE.md](CLAUDE.md) for the deep-dive on module layout, the long-poll notifier pattern, and the permission-relay protocol. Highlights:

- **In-process notifier only**: each peer machine has one server process, so cross-process notification is unnecessary.
- **Long-poll, not push**: the server does not push events to Claude Code. The model's worker loop must call `wait_for_instruction` repeatedly. The bundled `/worker` slash command sets that up.
- **MCP idle timeout**: the default `POLL_MAX_WAIT_S` is 25s to stay below Claude Code's MCP idle limit. Tune for your transport.
- **Future-aligned**: this design intentionally mirrors what Anthropic's [Channels](https://docs.claude.com/en/docs/claude-code/channels) feature is heading toward (push events into a running session, permission relay capability). When Channels stabilizes, a thin facade over this server can register as a channel plugin without changing the storage/tool layer.

## Reference architecture

Patterns (auth, logging, config, error envelope, start.sh, pyproject layout) are copied from [`fast-mcp-jira`](https://github.com/jeremy-newhouse/fast-mcp-jira) — the canonical FastMCP reference in this family.

## License

Private — not yet licensed for redistribution.
