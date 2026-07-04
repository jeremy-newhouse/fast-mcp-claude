# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

`fast-mcp-claude` is a FastMCP server that lets two (or more) Claude Code sessions on different machines control and coordinate each other. Each machine runs its own instance of this server; each Claude Code session points its `.mcp.json` at its **local** server (for the worker role) and any **remote** peers (for the controller role).

The architecture is intentionally peer-symmetric: every server stores its own inbox/outbox/permissions/pubsub state and exposes the same tools. There is no central hub.

## Commands

```bash
# Install (uv-managed venv)
uv sync --all-extras

# Run the server in the foreground
uv run fast-mcp-claude

# Run under pm2 (production)
./start.sh

# Tests
uv run pytest
uv run pytest tests/test_storage.py::test_reply_round_trip -v   # single test

# Lint / format
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

## Architecture

### Module layout

- `src/fast_mcp_claude/__main__.py` — CLI entry: setup logging, import `server.mcp`, run HTTP transport.
- `src/fast_mcp_claude/server.py` — Creates the `FastMCP` instance (`mcp`), `Store` instance (`store`), optional `ApiKeyVerifier`. Side-effect imports each `tools/` module at the bottom so their `@mcp.tool` decorators register.
- `src/fast_mcp_claude/config.py` — `Settings` (pydantic-settings) loaded from `.env`. Includes `PeerConfig` (per-peer URL+key), `workspace_roots_resolved` (sandbox), `poll_max_wait_s`, and the channel-sidecar opt-in (`channel_enabled` default `False`, `channel_identity`, `channel_summary`, plus `channel_decision_timeout_s`/`channel_reply_timeout_s`/`channel_auto_pass_tools` for the permission relay).
- `src/fast_mcp_claude/auth.py` — `ApiKeyVerifier` + `AuthRateLimiter` (timing-safe compare, lockout after N failures).
- `src/fast_mcp_claude/errors.py` — Error hierarchy (`ClaudeRemoteError`, `ValidationError`, `NotFoundError`, `PeerError`, `PermissionDeniedError`), `format_error_response()`, `build_response()`.
- `src/fast_mcp_claude/logging_config.py` — Dual formatters (JSON for prod, colored console for dev), redaction of sensitive field names, `@timed` decorator.
- `src/fast_mcp_claude/services/store.py` — SQLite-backed inbox/outbox/approvals/pubsub/presence. Contains the `Notifier` class (per-key `asyncio.Event`) that powers all long-poll tools.
- `src/fast_mcp_claude/tools/messaging.py` — `send_prompt`, `wait_for_completion`, `get_status`, `interrupt`, `cancel`, `list_messages`, `wait_for_instruction`, `reply`, `consume_interrupt`.
- `src/fast_mcp_claude/tools/permissions.py` — `request_approval` (hook-internal), `await_decision`, `pending_approvals`, `wait_for_pending_approval`, `approve_tool`.
- `src/fast_mcp_claude/tools/files.py` — `list_files`, `read_file`, `write_file` sandboxed to `WORKSPACE_ROOTS`.
- `src/fast_mcp_claude/tools/pubsub.py` — `publish`, `subscribe`.
- `src/fast_mcp_claude/tools/teams_outbox.py` — channel → Teams outbox (ADR-0013 in evolv-coder-agent): `request_teams_send`/`await_teams_send` (the channel asks the hub to post to Teams + awaits the result) and `wait_for_pending_teams_send`/`complete_teams_send` (the eCA hub drains + completes). A dedicated `teams_outbox` store queue, isolated from the message and approval queues. The hub decides whether to post (honors `metadata.triggering_admin`, resolves the chat name).
- `src/fast_mcp_claude/tools/session_relay.py` — session-to-session relay (ADR-0015 in evolv-coder-agent): `request_session_op`/`await_session_op` (a session asks the hub to `list` other sessions or `send` a message to one + awaits the result) and `wait_for_pending_session_ops`/`complete_session_op` (the eCA hub drains + routes + completes). A dedicated `session_relay` store queue carrying an opaque `{op, payload}` request + JSON result, isolated from the message/approval/teams queues. The hub does the cross-peer routing (peers can't reach each other; `who()` is server-local).
- `src/fast_mcp_claude/tools/presence.py` — `announce`, `who` (N-way peer discovery / roster; presence rows are heartbeated by the channel adapter).
- `src/fast_mcp_claude/utils/validation.py` — Format validators (session_id, identity, message_id, channel, peer_name), `validate_workspace_path` (sandbox + symlink-escape guard), body-size caps.
- `src/fast_mcp_claude/hook.py` — `fast-mcp-claude-hook` CLI; PreToolUse hook entry that talks to the local server via `fastmcp.Client`.
- `src/fast_mcp_claude/channel.py` — `fast-mcp-claude-channel` CLI; a two-way, permission-aware Claude Code **channel** sidecar (stdio, low-level `mcp.server.Server`) for **live interactive** sessions. It is the sole presence announcer (`role=live-session`, `channel:true`), long-polls the inbox and PUSHES each prompt as a `notifications/claude/channel` turn, exposes a `reply` tool (→ mesh `reply`), a `send_teams` tool (→ mesh `request_teams_send`/`await_teams_send`, so the session can post to a Teams chat via the hub; stamps `triggering_admin`/`conversation_id` from the in-flight task), and the **session-relay** tools `list_sessions`/`send_to_session`/`check_session_message` (→ mesh `request_session_op`/`await_session_op`, so the session can list + message the operator's OTHER sessions via the hub — ADR-0015). All of these own tools are auto-allowed in the relay (delivery/control paths), and it runs the **permission relay** (tees the stdio read stream to catch `claude/channel/permission_request`; admin→auto-allow, non-admin→Phase-3 approval). Subsumes `session.py` in channel mode. See the Channel push flow section below.

### Tool pattern

```python
from typing import Annotated, Any
from pydantic import Field
from ..errors import ValidationError, format_error_response
from ..server import mcp, settings, store
from ..utils.validation import validate_message_id

@mcp.tool(description="...")
async def some_tool(
    message_id: Annotated[str, Field(description="ID returned from send_prompt")],
) -> dict[str, Any]:
    try:
        message_id = validate_message_id(message_id)
        ...
        return {"success": True, "data": ...}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)
```

Always validate inputs first. Always return `{"success": bool, ...}`. Always catch `ValidationError` separately (it produces a 400-style response with `field`).

### Long-poll pattern

All blocking tools use `Notifier.wait_for(key, check, timeout)` from `services/store.py`:

```python
async def wait_for(self, key, check, timeout):
    ev = self._get(key)             # 1. capture event ref BEFORE checking DB
    if (result := await check()):   # 2. is there already data?
        return result
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    return await check()            # 3. re-check after wakeup
```

Producers call `self._notifier.notify(key)` after writing to the DB. The notifier sets the current event (waking any waiters) and replaces it with a fresh one for the next wait.

**Important**: notifications are in-process only. Different OS processes hitting the same SQLite file will not wake each other — but each peer machine has one server process, so this is fine.

### Permission relay flow

1. Worker's Claude attempts a tool → `PreToolUse` hook fires → invokes `fast-mcp-claude-hook`.
2. Hook calls local `request_approval(session_id, tool_name, tool_input)` → gets `approval_id` → calls `await_decision(approval_id, timeout=25)` in a loop until total timeout.
3. Controller's Claude (on a remote machine, hitting this worker's MCP server via `.mcp.json`) calls `wait_for_pending_approval()` or `pending_approvals()`, evaluates, calls `approve_tool(approval_id, decision, reason)`.
4. `await_decision` wakes → hook emits `{"hookSpecificOutput": {"permissionDecision": "allow|deny|ask", ...}}` to stdout → Claude Code respects it.

If the controller doesn't respond before `CRM_DECISION_TIMEOUT` (default 300s), the hook falls back to `"ask"` so the local user retains control.

### Channel push flow (`fast-mcp-claude-channel`)

The channel sidecar auto-delivers brain-sent prompts into a **live interactive** session (the eCA live-session arm), removing the manual `/fleet-inbox`. It is a separate stdio process Claude Code spawns (session launched with `--dangerously-load-development-channels server:fast-mcp-claude-channel`). **Proven on CC 2.1.168** (claude.ai auth — channels are not on Bedrock); see the eCA repo's ADR-0012 / `docs/architecture/channels-live-delivery.md`. It is a two-way, permission-aware **superset of `session.py`** for channel mode:

1. Sidecar starts a low-level `mcp.server.Server` over stdio and declares `experimental: {"claude/channel": {}, "claude/channel/permission": {}}` (+ a `reply` tool). Separate `fastmcp.Client` connections to the LOCAL HTTP server (same auth as the hook) for presence and inbox.
2. **Presence (sole announcer).** Heartbeats `announce(identity, summary, metadata)` with `role="live-session"` + **`channel: true`** (read from the hook-written status file). It SUBSUMES `session.py` in channel mode — never run both on one identity (announce is a full upsert; two announcers clobber).
3. **Inbound push.** Long-polls `wait_for_instruction(recipient_session=<identity>)` (the MCP idle timeout does not apply) and writes a `notifications/claude/channel` notification to the stdio write stream → the prompt appears as `<channel source="fast-mcp-claude-channel" message_id=... sender=...>`. ONE message in flight at a time (claim → push → await reply → next) to keep the permission correlation crisp. Exception (ECA-58): a hub-stamped fire-and-forget message (`metadata.expects_reply=false` — session-relay FYI notifies, broadcasts, late-reply push-backs) is pushed WITHOUT holding the in-flight slot and auto-acked on the mesh, so an unanswered FYI can't wedge the mailbox for `channel_reply_timeout_s` (30 min default).
4. **Outbound reply.** The agent calls the sidecar's `reply` tool → the sidecar calls mesh `reply(message_id, response)` → the controller's `wait_for_completion` unblocks. This is the agent's only reply path (it gets no `claude-local`; invariant 9).

**Permission relay (now works in Python).** The sidecar declares `claude/channel/permission` and receives `notifications/claude/channel/permission_request` by **teeing the stdio read stream** — the SDK's typed receive loop validates inbound notifications against `ClientNotification` and DROPS unknown methods, so we sniff each raw `JSONRPCNotification` before it reaches `Server.run()` (params survive intact at the stdio layer), intercept the permission request, and reply `notifications/claude/channel/permission` `{request_id, behavior}`. Routing by the in-flight message's `metadata.triggering_admin` (the eCA brain stamps it): admin → auto-allow; non-admin → open a Phase-3 `request_approval` on the local server (the brain's `ApprovalWatcher` DMs Jeremy) → `await_decision` → verdict, **default-deny** on TTL. The channel's own `reply` tool and `channel_auto_pass_tools` (read-only) auto-pass; a permission_request with no in-flight channel turn is the operator's own local work, left to the local terminal dialog. The relay fires only when a tool would open a dialog, so the session must run in `--permission-mode default` (the PreToolUse hook in `hook.py` remains the path for *launcher* headless workers).

**Coexistence & safety.** Channel mode is strict opt-in: `_resolve_config` resolves `enabled` as CLI `--enabled/--no-enabled` > `CHANNEL_ENABLED` env > `Settings.channel_enabled` (default `False`). When **disabled**, `_serve` still runs `_server.run(...)` so the MCP handshake succeeds (Claude Code spawns the sidecar for any wired `.mcp.json` entry, even without `--dangerously-load-development-channels`), but it starts **none** of the loops (`_presence_loop`/`_inbox_loop`/`_tee_reader`) — no `wait_for_instruction` poll, no inbox claim, no push, no relay. This is the key invariant: a wired-but-disabled sidecar must never claim a message and silently eat one destined for notify+pull (`/fleet-inbox`) — the controller's `wait_for_completion` would then hang until TTL. Arming requires BOTH `channel_enabled=true` AND the `--dangerously-load-development-channels` launch flag. In channel mode the channel sidecar is the **sole announcer**, so `start-session.sh` does NOT also run `session.py` (two announcers on one identity clobber each other). Identity precedence mirrors enabled: `--identity` > `CRM_IDENTITY` > `Settings.channel_identity` > `Settings.peer_name`.

## Security model

- **Mutual bearer auth**: each peer's `MCP_API_KEY` is shared with the other(s) via their `.mcp.json` / `PEERS` config. Timing-safe comparison; lockout after 5 failures in 5 minutes.
- **File-bridge sandbox**: `read_file`/`write_file` validate paths against `WORKSPACE_ROOTS` (colon-separated allowlist). Symlink escapes blocked via `Path.resolve(strict=False)` + `relative_to()` re-check. Null bytes and traversal patterns blocked at input.
- **No SSRF surface**: this server does not make outbound HTTP based on user input (no `peer_client` in v1). Adding any outbound HTTP capability requires re-introducing the SSRF allowlist pattern from `fast-mcp-jira`'s `config.py`.
- **Hook never silently denies**: any failure mode (server down, parse error, controller timeout) falls back to `permissionDecision: "ask"` so Claude Code's built-in permission UI takes over.
- **Body-size caps**: prompts ≤1MB, responses ≤4MB, files ≤10MB, pubsub payloads ≤256KB. Adjust in `utils/validation.py` if needed.

## Known limitations (v1)

- Single-process notification only — `Notifier` won't wake another process or machine. Each peer is its own asyncio loop. (Unnecessary in mesh; in a hub deployment everyone shares the one process anyway.)
- Worker priming: in **loop mode** the worker must be primed via `/worker`. **Channel mode** (`fast-mcp-claude-channel` + `--dangerously-load-development-channels`) removes this — tasks push in automatically — and is the recommended path.
- MCP idle timeout (~30s for stdio; longer for streamable-http) can silently kill a long `wait_for_instruction`. In loop mode keep `POLL_MAX_WAIT_S` ≤25s and call repeatedly; channel mode sidesteps it (the wait lives in the adapter process).
- Channels are a **research preview** (Claude Code ≥ v2.1.80) and need `--dangerously-load-development-channels` for a custom server; the notification API may change. Permission relay over `claude/channel/permission` is not yet implemented (inbound custom-notification gap in the Python MCP SDK) — the PreToolUse hook covers approvals.
- Mid-turn stdin injections in Claude Code's headless mode are not persisted to the session JSONL (anthropics/claude-code#41230). This server uses the `send_prompt`/channel paths, not stdin, so it isn't affected — but we complete a turn per reply rather than resuming `wait_for_instruction` inside the same turn.
