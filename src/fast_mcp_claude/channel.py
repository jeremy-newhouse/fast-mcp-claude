"""fast-mcp-claude-channel — Claude Code *channel* adapter (prompt push leg).

Claude Code spawns this as a stdio MCP subprocess (launch the worker session with
`claude --dangerously-load-development-channels server:claude-channel`). It declares
the experimental `claude/channel` capability, then bridges the LOCAL fast-mcp-claude
HTTP server's inbox into the live Claude session: it long-polls `wait_for_instruction`
and PUSHES each queued prompt as a `notifications/claude/channel` event.

Why this exists (see README "Channels"): it removes the two biggest v1 frictions —
  * the worker no longer needs the /worker long-poll loop to be primed by a human, and
  * the MCP idle timeout no longer applies, because the waiting happens in THIS
    process, not inside a Claude tool call.

Design notes:
  * Outbound only. The worker still returns results via the existing `reply` tool on
    its local server — channel pushes are fire-and-forget and unacknowledged, so the
    reply/outbox path remains the source of truth for delivery (the lesson from
    claude-peers-mcp's silent-message-loss bugs).
  * We run the low-level `mcp.server.Server` so ping/initialize/unknown-method are
    handled correctly, and push notifications by writing straight to the same stdio
    write stream the session uses (that is exactly what session.send_notification does).
  * Permission relay is NOT handled here. Claude Code's channel permission relay is an
    *inbound* custom notification, which the Python MCP SDK validates against the
    ClientNotification union and drops. Keep the PreToolUse hook (hook.py) for
    permissions; revisit channel permission relay when the SDK supports it.
  * Presence: heartbeats `announce(identity, summary)` so peers can discover this
    worker via `who`. Identity defaults to settings.peer_name and doubles as the
    inbox mailbox key (send_prompt(recipient_session=<identity>)).

Channel mode is STRICT opt-in. When disabled (the default) the adapter still
completes the MCP handshake — so Claude Code stays happy even though the entry is
wired in `.mcp.json` — but it does NOT poll, claim, or push. This is the safety
switch that lets a configured-but-unintended adapter coexist with `/worker` loop
mode without eating inbox messages. Arming requires BOTH `channel_enabled` (env or
Settings) AND launching with `--dangerously-load-development-channels`.

Config (CLI flag, else env, else Settings/.env default):
    --enabled/--no-enabled / CHANNEL_ENABLED   arm the poll/push bridge
                                     (default: Settings.channel_enabled, off)
    --identity   / CRM_IDENTITY      mailbox + presence identity
                                     (default: channel_identity, else peer_name)
    --local-url  / CRM_LOCAL_URL     local server MCP URL (default http://127.0.0.1:<port>/mcp)
                   MCP_API_KEY        bearer for the local server (if it requires auth)
    --summary    / CRM_SUMMARY       one-line presence blurb shown by who()
                                     (default: channel_summary)
    --poll       / CRM_POLL_S        long-poll seconds per wait_for_instruction (default 25)
    --heartbeat  / CRM_HEARTBEAT_S   presence heartbeat seconds (default 20)
                   CRM_CHANNEL_DEBUG set to "0" to silence stderr diagnostics
"""

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any

import anyio
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from . import __version__

CHANNEL_METHOD = "notifications/claude/channel"
SERVER_NAME = "fast-mcp-claude"

# Startup grace before the first push: the MCP handshake completes in well under a
# second, while a controller's first message is human-speed (seconds) away — so this
# is ample to avoid pushing into an un-initialized session (which drops events).
INIT_GRACE_S = 1.0

INSTRUCTIONS = (
    "You are a fast-mcp-claude WORKER reachable over a peer channel. Remote "
    "controllers push tasks to you as "
    '<channel source="fast-mcp-claude" message_id="..." sender="..."> events.\n'
    "When you receive one:\n"
    "  1. Treat the channel body as a normal user request and carry it out in this repo.\n"
    "  2. When finished (or on unrecoverable error), call the `reply` tool on your local "
    "fast-mcp-claude server, passing the message_id from the tag and a thorough result.\n"
    "Channel delivery is fire-and-forget: the controller only sees your work after you "
    "call reply, so ALWAYS reply — even to report a failure. You do not need to poll for "
    "work; tasks arrive automatically."
)

# Low-level server: handles initialize/ping/unknown-method correctly. We register no
# tools — this is a one-way (push) channel; the worker replies via the HTTP server.
_server: Server = Server(SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)


def _log(msg: str) -> None:
    # stderr only — stdout is the MCP stdio transport. Lands in ~/.claude/debug/.
    if os.environ.get("CRM_CHANNEL_DEBUG", "1") != "0":
        print(f"[fast-mcp-claude-channel] {msg}", file=sys.stderr, flush=True)


@dataclass
class ChannelConfig:
    identity: str
    local_url: str
    api_key: str | None
    summary: str | None
    poll: float
    heartbeat: float
    enabled: bool


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "t", "y")


def _resolve_config(argv: list[str]) -> ChannelConfig:
    p = argparse.ArgumentParser(prog="fast-mcp-claude-channel")
    p.add_argument("--identity", default=None)
    p.add_argument("--local-url", default=None)
    p.add_argument("--summary", default=None)
    p.add_argument("--poll", type=float, default=None)
    p.add_argument("--heartbeat", type=float, default=None)
    p.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Arm the poll/push bridge. Default comes from CHANNEL_ENABLED env, "
            "else Settings.channel_enabled (off). When off the adapter completes "
            "the MCP handshake but stays inert — no polling, no inbox claims, no push."
        ),
    )
    args = p.parse_args(argv)

    # Defaults from the project Settings/.env when available; the adapter and the
    # server are normally launched from the same directory with the same config.
    peer_name, port, api_key, poll, heartbeat = "default", 5473, None, 25.0, 20.0
    channel_enabled, channel_identity, channel_summary = False, None, None
    try:
        from .config import get_settings

        s = get_settings()
        peer_name = s.peer_name or peer_name
        port = s.mcp_port
        api_key = s.mcp_api_key
        poll = float(s.poll_max_wait_s)
        heartbeat = float(s.poll_heartbeat_s)
        channel_enabled = s.channel_enabled
        channel_identity = s.channel_identity
        channel_summary = s.channel_summary
    except Exception as e:  # bad/missing .env shouldn't kill the adapter
        _log(f"settings unavailable, using bare defaults: {e}")

    # Precedence (highest first): CLI flag > env var > Settings default.
    identity = args.identity or os.environ.get("CRM_IDENTITY") or channel_identity or peer_name
    local_url = args.local_url or os.environ.get("CRM_LOCAL_URL") or f"http://127.0.0.1:{port}/mcp"
    api_key = os.environ.get("MCP_API_KEY", api_key)
    summary = args.summary or os.environ.get("CRM_SUMMARY") or channel_summary
    poll = args.poll if args.poll is not None else _env_float("CRM_POLL_S", poll)
    heartbeat = (
        args.heartbeat if args.heartbeat is not None else _env_float("CRM_HEARTBEAT_S", heartbeat)
    )
    enabled = (
        args.enabled if args.enabled is not None else _env_bool("CHANNEL_ENABLED", channel_enabled)
    )
    return ChannelConfig(
        identity=identity,
        local_url=local_url,
        api_key=api_key,
        summary=summary,
        poll=poll,
        heartbeat=heartbeat,
        enabled=enabled,
    )


def _result_data(result: Any) -> dict[str, Any]:
    """Extract a structured tool result across fastmcp.Client versions (see hook.py)."""
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
    return {}


async def _push(write_stream: Any, msg: dict[str, Any]) -> None:
    """Push one inbox message into the live Claude session as a channel event.

    Meta keys must be identifiers (letters/digits/underscore) or Claude Code drops
    them — message_id / sender / recipient all qualify.
    """
    meta: dict[str, str] = {
        "message_id": str(msg.get("id", "")),
        "sender": str(msg.get("sender") or "peer"),
    }
    if msg.get("recipient_session"):
        meta["recipient"] = str(msg["recipient_session"])
    notif = JSONRPCNotification(
        jsonrpc="2.0",
        method=CHANNEL_METHOD,
        params={"content": str(msg.get("prompt", "")), "meta": meta},
    )
    await write_stream.send(SessionMessage(message=JSONRPCMessage(notif)))


async def _bridge(write_stream: Any, cfg: ChannelConfig) -> None:
    """Connect to the local server and pump inbox -> channel until cancelled."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    await asyncio.sleep(INIT_GRACE_S)

    def _make_client() -> Client:
        # fastmcp 3.x: Client() no longer takes a `headers=` kwarg; the bearer rides
        # on the transport (mirrors evolv-coder-agent fleet.py + launcher.py).
        if cfg.api_key:
            transport = StreamableHttpTransport(
                cfg.local_url, headers={"Authorization": f"Bearer {cfg.api_key}"}
            )
            return Client(transport)
        return Client(cfg.local_url)

    backoff = 1.0
    last_announce = 0.0
    while True:
        try:
            async with _make_client() as c:
                backoff = 1.0
                _log(f"connected to {cfg.local_url} as identity={cfg.identity!r}")
                while True:
                    if time.time() - last_announce >= cfg.heartbeat:
                        try:
                            await c.call_tool(
                                "announce",
                                {"identity": cfg.identity, "summary": cfg.summary},
                            )
                        except Exception as e:
                            _log(f"announce failed (continuing): {e}")
                        last_announce = time.time()

                    res = await c.call_tool(
                        "wait_for_instruction",
                        {"recipient_session": cfg.identity, "timeout": cfg.poll},
                    )
                    data = _result_data(res)
                    if not data.get("success"):
                        _log(f"wait_for_instruction returned error: {data}")
                        await asyncio.sleep(1.0)
                        continue
                    msg = data.get("message")
                    if msg:
                        await _push(write_stream, msg)
                        _log(f"pushed message {msg.get('id')} from {msg.get('sender')}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"bridge error: {e}; reconnecting in {backoff:.0f}s\n{traceback.format_exc()}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def _serve(cfg: ChannelConfig) -> None:
    init_options = _server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={"claude/channel": {}},
    )
    if not cfg.enabled:
        # SAFETY: wired-but-disabled. Complete the MCP handshake so Claude Code is
        # happy, but DO NOT start the bridge — no polling, no inbox claims, no push.
        # A disabled adapter that polled would mark messages "delivered" and push
        # them into a channel nobody is listening to (silent message loss); leaving
        # it inert lets /worker loop mode own the inbox. Arming requires
        # CHANNEL_ENABLED=true AND --dangerously-load-development-channels.
        _log("channel disabled (CHANNEL_ENABLED=false); inert — use /worker loop mode.")
        async with stdio_server() as (read_stream, write_stream):
            await _server.run(read_stream, write_stream, init_options)
        return

    _log(f"starting channel adapter (identity={cfg.identity!r}, local={cfg.local_url})")
    async with stdio_server() as (read_stream, write_stream):
        async with anyio.create_task_group() as tg:
            # Push loop shares the session's stdout write stream; the server loop owns
            # the protocol (handshake, ping, shutdown when stdin closes).
            tg.start_soon(_bridge, write_stream, cfg)
            await _server.run(read_stream, write_stream, init_options)
            tg.cancel_scope.cancel()


def main() -> None:
    cfg = _resolve_config(sys.argv[1:])
    try:
        anyio.run(_serve, cfg)
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    main()
