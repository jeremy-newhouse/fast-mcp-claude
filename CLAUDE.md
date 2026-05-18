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
- `src/fast_mcp_claude/config.py` — `Settings` (pydantic-settings) loaded from `.env`. Includes `PeerConfig` (per-peer URL+key), `workspace_roots_resolved` (sandbox), `poll_max_wait_s` etc.
- `src/fast_mcp_claude/auth.py` — `ApiKeyVerifier` + `AuthRateLimiter` (timing-safe compare, lockout after N failures).
- `src/fast_mcp_claude/errors.py` — Error hierarchy (`ClaudeRemoteError`, `ValidationError`, `NotFoundError`, `PeerError`, `PermissionDeniedError`), `format_error_response()`, `build_response()`.
- `src/fast_mcp_claude/logging_config.py` — Dual formatters (JSON for prod, colored console for dev), redaction of sensitive field names, `@timed` decorator.
- `src/fast_mcp_claude/services/store.py` — SQLite-backed inbox/outbox/approvals/pubsub. Contains the `Notifier` class (per-key `asyncio.Event`) that powers all long-poll tools.
- `src/fast_mcp_claude/tools/messaging.py` — `send_prompt`, `wait_for_completion`, `get_status`, `interrupt`, `cancel`, `list_messages`, `wait_for_instruction`, `reply`, `consume_interrupt`.
- `src/fast_mcp_claude/tools/permissions.py` — `request_approval` (hook-internal), `await_decision`, `pending_approvals`, `wait_for_pending_approval`, `approve_tool`.
- `src/fast_mcp_claude/tools/files.py` — `list_files`, `read_file`, `write_file` sandboxed to `WORKSPACE_ROOTS`.
- `src/fast_mcp_claude/tools/pubsub.py` — `publish`, `subscribe`.
- `src/fast_mcp_claude/utils/validation.py` — Format validators (session_id, message_id, channel, peer_name), `validate_workspace_path` (sandbox + symlink-escape guard), body-size caps.
- `src/fast_mcp_claude/hook.py` — `fast-mcp-claude-hook` CLI; PreToolUse hook entry that talks to the local server via `fastmcp.Client`.

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

## Security model

- **Mutual bearer auth**: each peer's `MCP_API_KEY` is shared with the other(s) via their `.mcp.json` / `PEERS` config. Timing-safe comparison; lockout after 5 failures in 5 minutes.
- **File-bridge sandbox**: `read_file`/`write_file` validate paths against `WORKSPACE_ROOTS` (colon-separated allowlist). Symlink escapes blocked via `Path.resolve(strict=False)` + `relative_to()` re-check. Null bytes and traversal patterns blocked at input.
- **No SSRF surface**: this server does not make outbound HTTP based on user input (no `peer_client` in v1). Adding any outbound HTTP capability requires re-introducing the SSRF allowlist pattern from `fast-mcp-jira`'s `config.py`.
- **Hook never silently denies**: any failure mode (server down, parse error, controller timeout) falls back to `permissionDecision: "ask"` so Claude Code's built-in permission UI takes over.
- **Body-size caps**: prompts ≤1MB, responses ≤4MB, files ≤10MB, pubsub payloads ≤256KB. Adjust in `utils/validation.py` if needed.

## Known limitations (v1)

- Single-process notification only — `Notifier` won't wake another process or machine. Each peer is its own asyncio loop.
- The worker loop must be primed via a slash command (`/worker`) — Claude doesn't auto-poll without instruction.
- Mid-turn stdin injections in Claude Code's headless mode are not persisted to the session JSONL (anthropics/claude-code#41230). This server uses the `send_prompt` tool path, not stdin, so it isn't affected — but the implication is that we cannot resume a `wait_for_instruction` inside the same Claude turn that received a message; replies must complete the turn.
- MCP idle timeout (~30s for stdio; longer for streamable-http) means `wait_for_instruction(timeout=300)` may be silently killed. Tune `POLL_MAX_WAIT_S` and have the worker loop call repeatedly.
