"""fast-mcp-claude-channel — Claude Code *channel* sidecar for LIVE interactive sessions.

Claude Code spawns this as a stdio MCP subprocess (launch the session with
`claude --dangerously-load-development-channels server:fast-mcp-claude-channel`). It is the
push (auto-delivery) replacement for the notify+pull `session.py` sidecar: instead of firing
a macOS notification and waiting for the operator to run `/fleet-inbox`, it PUSHES each
brain-sent prompt straight into the live session as a `<channel>` turn and routes the
session's reply back over the mesh — fully automatic, with the operator still watching every
turn in their own terminal.

What it does (all against the LOCAL fast-mcp-claude HTTP server, which the brain reaches over
a forward SSH tunnel):

  1. PRESENCE (sole announcer). Heartbeats `announce(identity, summary, metadata)` with
     role="live-session" + `channel: true` (so the brain discovers push-capability and skips
     notify+pull). It SUBSUMES `session.py` for channel mode — do NOT run both on one identity
     (announce() is a full upsert; two announcers clobber each other). Presence metadata is
     read from the same hook-written status file `session.py` uses.

  2. INBOUND push. Long-polls `wait_for_instruction(recipient_session=identity)` — an
     automated `/fleet-inbox` — and PUSHES each claimed message as a `notifications/claude/
     channel` event, carrying `message_id` in `meta` so the agent can echo it back. ONE
     message in flight at a time (claim -> push -> await reply -> next): the live session runs
     one turn at a time anyway, and a single in-flight turn keeps the permission correlation
     (below) crisp.

  3. OUTBOUND reply tool. Exposes an MCP `reply` tool; when the agent calls it the sidecar
     calls mesh `reply(message_id, response)` -> unblocks the brain's `wait_for_completion` ->
     the brain delivers to Teams. This is the agent's ONLY reply path (it never sees the mesh
     worker verbs — invariant 9).

  4. PERMISSION RELAY + approval routing. Declares `claude/channel/permission`. When a tool
     call in a channel turn opens a permission dialog, Claude Code sends
     `notifications/claude/channel/permission_request` and the sidecar decides:
       * our own reply tool + read-only tools (channel_auto_pass_tools) -> allow (no round-trip);
       * the in-flight message was triggered by an ADMIN (brain stamps metadata.triggering_admin
         = true on an admin-authorized turn) -> allow immediately (zero friction);
       * otherwise (non-admin / unknown channel sender) -> mesh `request_approval` ->
         the brain's Phase-3 `ApprovalWatcher` DMs Jeremy in Teams -> `approve_tool` ->
         `await_decision` -> verdict. Default DENY on timeout (never auto-approve).
     A permission_request with NO in-flight channel turn is the operator's OWN local work:
     the sidecar stays silent and the local terminal dialog handles it.

Why the SDK can't receive the permission notification directly: the MCP SDK's typed receive
loop validates inbound notifications against `ClientNotification` and DROPS unknown methods.
We TEE the raw stdio read stream — sniffing each `JSONRPCNotification` before the typed loop
(params survive intact at the stdio layer), intercepting the permission_request, and
forwarding everything else to `Server.run()` unchanged.

Channel mode is STRICT opt-in. When disabled (the default) the adapter completes the MCP
handshake — so Claude Code stays happy even though the entry is wired in `.mcp.json` — but
does NOT poll, claim, push, or relay. Arming requires BOTH `channel_enabled` (env/Settings)
AND launching with `--dangerously-load-development-channels`.

Config (CLI flag, else env, else Settings/.env default):
    --enabled/--no-enabled / CHANNEL_ENABLED   arm the bridge (default Settings.channel_enabled)
    --identity   / CRM_IDENTITY      mailbox + presence identity (default channel_identity/peer)
    --local-url  / CRM_LOCAL_URL     local server MCP URL (default http://127.0.0.1:<port>/mcp)
                   MCP_API_KEY        bearer for the local server
    --summary    / CRM_SUMMARY       fallback presence blurb (default channel_summary)
    --status-file/ CRM_SESSION_STATUS_FILE   JSON status file the CC hooks write (presence metadata)
    --poll       / CRM_POLL_S        wait_for_instruction long-poll seconds (default 25)
    --heartbeat  / CRM_HEARTBEAT_S   presence heartbeat seconds (default 20)
    --decision-timeout / CHANNEL_DECISION_TIMEOUT_S  non-admin await_decision budget (default 300)
    --reply-timeout    / CHANNEL_REPLY_TIMEOUT_S     max wait for the agent's reply before
                                     claiming the next message (default 1800)
    --auto-pass        / CHANNEL_AUTO_PASS_TOOLS     comma tools allowed w/o a Teams round-trip
                                     even on non-admin turns (default Read,Glob,Grep)
                   CRM_CHANNEL_DEBUG set to "0" to silence stderr diagnostics
"""

import argparse
import asyncio
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from . import __version__

CHANNEL_METHOD = "notifications/claude/channel"
PERM_REQUEST = "notifications/claude/channel/permission_request"
PERM_REPLY = "notifications/claude/channel/permission"
INITIALIZED = "notifications/initialized"
# The MCP server name == the .mcp.json key == the `server:<name>` dev-channels flag arg, so the
# agent's reply tool is mcp__fast-mcp-claude-channel__reply and we can auto-allow it by name.
SERVER_NAME = "fast-mcp-claude-channel"
OUR_REPLY_TOOL = f"mcp__{SERVER_NAME}__reply"
OUR_SEND_TEAMS_TOOL = f"mcp__{SERVER_NAME}__send_teams"
OUR_LIST_SESSIONS_TOOL = f"mcp__{SERVER_NAME}__list_sessions"
OUR_SEND_TO_SESSION_TOOL = f"mcp__{SERVER_NAME}__send_to_session"
OUR_CHECK_SESSION_MESSAGE_TOOL = f"mcp__{SERVER_NAME}__check_session_message"
# Our own tools are delivery/control paths (like reply): always allow the CALL — the hub
# re-applies any policy on its side. Kept as a set so the permission relay + auto-pass agree.
OUR_TOOLS = frozenset(
    {
        OUR_REPLY_TOOL,
        OUR_SEND_TEAMS_TOOL,
        OUR_LIST_SESSIONS_TOOL,
        OUR_SEND_TO_SESSION_TOOL,
        OUR_CHECK_SESSION_MESSAGE_TOOL,
    }
)

INSTRUCTIONS_BASE = (
    "You are a fast-mcp-claude LIVE session reachable over a peer channel. The eCA brain "
    "pushes tasks to you as "
    '<channel source="fast-mcp-claude-channel" message_id="..." sender="..."> events.\n'
    "When you receive one:\n"
    "  1. Treat the channel body as a normal user request and carry it out in this repo.\n"
    "  2. When finished (or on unrecoverable error), call the `reply` tool with the EXACT "
    "`message_id` from the channel tag and a thorough `response`.\n"
    "Channel delivery is fire-and-forget: the controller only sees your work after you call "
    "reply, so ALWAYS reply — even to report a failure. Tasks arrive automatically; you never "
    "need to poll for work.\n"
    "To post a message to a Microsoft Teams chat via the eCA hub, call the `send_teams` tool "
    "(`text`, optional `target` chat name — omit it to post back to the chat that sent you this "
    "task). It returns whether the hub delivered it."
)

# Teams formatting conventions. These live in the server `instructions` (not in a skill) because
# the instructions are the ONLY context guaranteed present in EVERY channel-handling session, on
# EVERY peer and in EVERY repo. A pushed task reads as a generic repo job, so nothing else flags
# the eventual send_teams / relayed reply as Teams-bound — without this block the rules would only
# fire in repos that happen to carry a Teams skill. Keep this the GENERIC, repo-agnostic subset;
# repo-specific detail (destination chat names, JIRA host, repo/org names, any message preface)
# stays in the working repo's own Teams skill, which this block tells the session to load.
# The one concrete name kept inline — the operator `Jeremy Newhouse` — is deployment-universal,
# NOT repo-specific: the brain has a single admin/approver across the whole fleet (invariant 8;
# the ApprovalWatcher DMs that same person), and send_teams needs a real, resolvable target, so a
# placeholder would be unactionable here.
_TEAMS_FORMATTING = """\
## Teams formatting (MANDATORY)

Apply before EVERY send_teams call, and before any reply whose content will be
relayed to a Teams chat. If the working repo provides a Teams formatting skill
(e.g. .claude/skills/teams-ultra-chat), load it for the repo-specific DATA it
defines — destination chat names, JIRA host, and repo/org names. The rules below
are the non-negotiable subset and OVERRIDE any conflicting general etiquette in
that skill (e.g. a message preface); they apply on EVERY peer and in EVERY repo,
even when no such skill is present.

- Tables: use Markdown pipe-tables — a header row, a `| --- |` separator row, and
  EACH data row on its OWN line. NEVER collapse a table onto a single line (it
  renders as raw text — the most common failure). The hub renders pipe-tables as
  real grids, including `[text](url)` links inside cells (verified 2026-06-08).
- Links: every JIRA key, PR number, commit SHA, and URL must be a Markdown link,
  never bare — e.g. `[ABC-34](https://<jira-host>/browse/ABC-34)` or
  `[be#187](https://github.com/<org>/<repo>/pull/187)`. Take the JIRA host, repo,
  and org from the WORKING repo (its git remotes / its Teams skill), never from
  memory or another repo. Commit links require the FULL 40-char SHA (get it via
  `git rev-parse <short-sha>`; a fabricated suffix 404s silently). Link refs even
  inside table cells — a bare key in a cell is not acceptable.
- No emojis, ever. No "From Claude Code" preface (this is a dedicated agent
  identity). No confidential client data. Push commits to the remote BEFORE
  linking them.
- Route by audience — pick ONE destination per message. Project/team work updates
  go to the team chat the working repo's Teams skill names; do NOT guess a chat
  name, and do NOT post one repo's updates to another repo's chat. Admin
  approvals, decisions, and sensitive / 1:1 items go to the operator
  (`Jeremy Newhouse`). When unsure between a team chat and the operator for
  anything needing approval or holding sensitive detail → the operator.
- Never mention peer-machine names, IPs, hostnames, or runtime/environment detail
  in a team chat (those may go to the operator only, when relevant).
"""

# Session-to-session messaging (ADR-0015). These tools let the operator's live sessions stay
# in sync with each other. They are relayed by the eCA hub (the only node that can reach every
# peer); a message into another session arrives as a normal turn prefixed
# "[Session message from <identity>]".
_SESSION_MESSAGING = """\
## Talking to the operator's other sessions

You can see and message the operator's OTHER live Claude Code sessions (across all their
machines) to keep work in sync. The eCA hub relays this — you never address peers directly.

- `list_sessions()` — list the operator's other live sessions: machine, repo, branch, what
  each is doing (status), and whether it is channel-push capable. Use this to answer "what is
  everyone working on?" or to find a target's address before sending.
- `send_to_session(target, text, wait_for_reply=false)` — deliver a message to another session.
  - `target` is `machine.repo` (e.g. `mbpm2.backend`) from list_sessions, or `"all"` to send to
    every other live session at once (a broadcast — e.g. "I'm about to deploy, pause pushes").
  - Default is fire-and-forget (an FYI). Set `wait_for_reply=true` (single target only) to block
    for the other session's answer; if it doesn't answer within the wait budget the message is
    still delivered and its reply will be pushed back to you when it lands.

Use these for coordination ("I changed the API contract in <repo>, rebase", "what's your
branch?"). The receiving session sees your message as a normal turn and decides what to do; a
relayed message does NOT get elevated tool permissions on the other side.
"""

INSTRUCTIONS = (
    INSTRUCTIONS_BASE
    + "\n\n"
    + _SESSION_MESSAGING.rstrip()
    + "\n\n"
    + _TEAMS_FORMATTING.rstrip()
)

_server: Server = Server(SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)


def _log(msg: str) -> None:
    # stderr only — stdout is the MCP stdio transport. Lands in ~/.claude/debug/.
    if os.environ.get("CRM_CHANNEL_DEBUG", "1") != "0":
        print(f"[fast-mcp-claude-channel] {msg}", file=sys.stderr, flush=True)


# A bad bearer locks the WHOLE mesh endpoint for 60s after 5 attempts, which would also lock out
# the legitimately-authed hook/launcher on the same server — so on an auth error cool down LONGER
# than the lockout instead of retry-storming it on the normal 1→30s reconnect backoff (mirrors
# session.py; see CLAUDE.md "never retry-storm auth failures").
_AUTH_HINTS = ("401", "403", "unauthorized", "forbidden")
_AUTH_COOLDOWN_S = 90.0


async def _reconnect_sleep(what: str, exc: Exception, backoff: float) -> float:
    """Shared reconnect handler for the presence/inbox loops: auth → long cooldown, else backoff."""
    if any(h in str(exc).lower() for h in _AUTH_HINTS):
        _log(f"{what} AUTH error: {exc}; cooling down {_AUTH_COOLDOWN_S:.0f}s (no retry-storm)")
        await asyncio.sleep(_AUTH_COOLDOWN_S)
        return 1.0
    _log(f"{what} error: {exc}; reconnecting in {backoff:.0f}s")
    await asyncio.sleep(backoff)
    return min(backoff * 2, 30.0)


@dataclass
class ChannelConfig:
    identity: str
    local_url: str
    api_key: str | None
    summary: str | None
    poll: float
    heartbeat: float
    enabled: bool
    status_file: str | None = None
    decision_timeout: float = 300.0
    reply_timeout: float = 1800.0
    auto_pass_tools: frozenset[str] = frozenset({"Read", "Glob", "Grep"})


@dataclass
class _Runtime:
    """Shared mutable state between the inbox loop, the reply tool, and the permission relay.

    All live on the single asyncio loop, so plain attributes (no locks) are race-free; the
    only cross-task signal is `reply_event` (set by the reply tool, awaited by the inbox loop).
    """

    cfg: ChannelConfig
    inflight: dict[str, Any] | None = None  # the claimed message currently pushed, awaiting reply
    reply_event: asyncio.Event = field(default_factory=asyncio.Event)
    initialized: asyncio.Event = field(default_factory=asyncio.Event)


# Populated by _serve before any loop or tool handler runs.
_RT: _Runtime | None = None


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


def _parse_tools(raw: str | None) -> frozenset[str] | None:
    if raw is None:
        return None
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _resolve_config(argv: list[str]) -> ChannelConfig:
    p = argparse.ArgumentParser(prog="fast-mcp-claude-channel")
    p.add_argument("--identity", default=None)
    p.add_argument("--local-url", default=None)
    p.add_argument("--summary", default=None)
    p.add_argument("--status-file", default=None)
    p.add_argument("--poll", type=float, default=None)
    p.add_argument("--heartbeat", type=float, default=None)
    p.add_argument("--decision-timeout", type=float, default=None)
    p.add_argument("--reply-timeout", type=float, default=None)
    p.add_argument("--auto-pass", default=None)
    p.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Arm the push/relay bridge. Default comes from CHANNEL_ENABLED env, else "
            "Settings.channel_enabled (off). When off the adapter completes the MCP handshake "
            "but stays inert — no polling, no inbox claims, no push, no permission relay."
        ),
    )
    args = p.parse_args(argv)

    # Defaults from the project Settings/.env when available; the adapter and the server are
    # normally launched from the same directory with the same config.
    peer_name, port, api_key, poll, heartbeat = "default", 5473, None, 25.0, 20.0
    channel_enabled, channel_identity, channel_summary = False, None, None
    decision_timeout, reply_timeout = 300.0, 1800.0
    auto_pass = frozenset({"Read", "Glob", "Grep"})
    status_file_default = None
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
        decision_timeout = float(getattr(s, "channel_decision_timeout_s", decision_timeout))
        reply_timeout = float(getattr(s, "channel_reply_timeout_s", reply_timeout))
        ap = _parse_tools(getattr(s, "channel_auto_pass_tools", None))
        if ap is not None:
            auto_pass = ap
        status_file_default = getattr(s, "session_status_file", "") or None
    except Exception as e:  # bad/missing .env shouldn't kill the adapter
        _log(f"settings unavailable, using bare defaults: {e}")

    # Precedence (highest first): CLI flag > env var > Settings default.
    identity = args.identity or os.environ.get("CRM_IDENTITY") or channel_identity or peer_name
    local_url = args.local_url or os.environ.get("CRM_LOCAL_URL") or f"http://127.0.0.1:{port}/mcp"
    api_key = os.environ.get("MCP_API_KEY", api_key)
    summary = args.summary or os.environ.get("CRM_SUMMARY") or channel_summary
    status_file = (
        args.status_file or os.environ.get("CRM_SESSION_STATUS_FILE") or status_file_default
    )
    poll = args.poll if args.poll is not None else _env_float("CRM_POLL_S", poll)
    heartbeat = (
        args.heartbeat if args.heartbeat is not None else _env_float("CRM_HEARTBEAT_S", heartbeat)
    )
    decision_timeout = (
        args.decision_timeout
        if args.decision_timeout is not None
        else _env_float("CHANNEL_DECISION_TIMEOUT_S", decision_timeout)
    )
    reply_timeout = (
        args.reply_timeout
        if args.reply_timeout is not None
        else _env_float("CHANNEL_REPLY_TIMEOUT_S", reply_timeout)
    )
    # Precedence CLI > env > Settings, using `is not None` per source — NOT `or`: an explicit
    # `--auto-pass ""` parses to an (intentionally) falsy empty frozenset, and `or` would wrongly
    # fall through to the env/default, silently ignoring the operator's "auto-pass nothing" opt-out.
    ap_cli = _parse_tools(args.auto_pass)
    ap = ap_cli if ap_cli is not None else _parse_tools(os.environ.get("CHANNEL_AUTO_PASS_TOOLS"))
    if ap is not None:
        auto_pass = ap
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
        status_file=status_file,
        decision_timeout=decision_timeout,
        reply_timeout=reply_timeout,
        auto_pass_tools=auto_pass,
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


def _make_client(cfg: ChannelConfig, timeout: float | None = None) -> Any:
    """A fastmcp Client to the LOCAL server. The bearer rides on the transport (fastmcp 3.x
    dropped the Client(headers=) kwarg), mirroring evolv-coder-agent fleet.py + launcher.py."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    if cfg.api_key:
        transport = StreamableHttpTransport(
            cfg.local_url, headers={"Authorization": f"Bearer {cfg.api_key}"}
        )
        return Client(transport, timeout=timeout)
    return Client(cfg.local_url, timeout=timeout)


# ------------------------------------------------------------------ presence (subsumes session.py)


def _read_status(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _build_presence(cfg: ChannelConfig) -> tuple[str | None, dict[str, Any]]:
    """Summary + metadata for announce(), from the hook-written status file. Mirrors
    session.py._build_presence but stamps `channel: true` so the brain pushes (no notify+pull)."""
    st = _read_status(cfg.status_file)
    status = st.get("status") or "active"
    last = (st.get("last") or "").strip()
    repo = st.get("repo") or ""
    summary = f"{repo or cfg.identity} [{status}]"
    if last:
        summary = f"{summary} — {last}"
    summary = summary[:280]
    if not (repo or last) and cfg.summary:
        summary = cfg.summary[:280]
    meta: dict[str, Any] = {
        "role": "live-session",
        "channel": True,
        "machine": st.get("machine"),
        "repo": repo or None,
        "cwd": st.get("cwd"),
        "branch": st.get("branch"),
        "status": status,
        "last": last or None,
        "status_updated_at": st.get("updated_at"),
    }
    return summary, {k: v for k, v in meta.items() if v is not None}


async def _presence_loop(cfg: ChannelConfig) -> None:
    """Heartbeat announce() on its own connection until cancelled (session exit)."""
    backoff = 1.0
    warned_announce = False
    while True:
        try:
            async with _make_client(cfg) as c:
                backoff = 1.0
                while True:
                    summary, meta = _build_presence(cfg)
                    try:
                        res = await c.call_tool(
                            "announce",
                            {"identity": cfg.identity, "summary": summary, "metadata": meta},
                        )
                        data = _result_data(res)
                        if not data.get("success") and not warned_announce:
                            _log(
                                f"announce REJECTED for identity={cfg.identity!r} "
                                f"(this session is INVISIBLE to the brain): {data}"
                            )
                            warned_announce = True
                    except Exception as e:
                        _log(f"announce failed (continuing): {e}")
                    await asyncio.sleep(cfg.heartbeat)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            backoff = await _reconnect_sleep("presence", e, backoff)


# --------------------------------------------------------------------- inbound push


async def _push(write_stream: Any, msg: dict[str, Any]) -> None:
    """Push one claimed inbox message into the live session as a channel event.

    Meta keys must be identifiers (letters/digits/underscore) or Claude Code drops them —
    message_id / sender / recipient all qualify.
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


async def _inbox_loop(cfg: ChannelConfig, write_stream: Any) -> None:
    """Claim one message at a time -> push -> await its reply -> claim the next."""
    assert _RT is not None
    rt = _RT
    # Don't push before the session has finished initializing (events into an un-initialized
    # session are dropped). The tee sets this when it sees notifications/initialized.
    try:
        await asyncio.wait_for(rt.initialized.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        _log("session did not signal initialized within 30s; proceeding anyway")

    backoff = 1.0
    while True:
        try:
            async with _make_client(cfg) as c:
                backoff = 1.0
                _log(f"inbox bridge connected to {cfg.local_url} as identity={cfg.identity!r}")
                while True:
                    res = await c.call_tool(
                        "wait_for_instruction",
                        {"recipient_session": cfg.identity, "timeout": cfg.poll},
                    )
                    data = _result_data(res)
                    if not data.get("success"):
                        _log(f"wait_for_instruction error: {data}")
                        await asyncio.sleep(1.0)
                        continue
                    msg = data.get("message")
                    if not msg:
                        continue  # long-poll timeout, no message — loop again
                    rt.inflight = msg
                    rt.reply_event = asyncio.Event()
                    await _push(write_stream, msg)
                    _log(f"pushed message {msg.get('id')} from {msg.get('sender')}; awaiting reply")
                    try:
                        await asyncio.wait_for(rt.reply_event.wait(), timeout=cfg.reply_timeout)
                    except asyncio.TimeoutError:
                        _log(
                            f"no channel reply for {msg.get('id')} in {cfg.reply_timeout:.0f}s; "
                            "claiming next (a late reply still lands via the reply tool)"
                        )
                    finally:
                        rt.inflight = None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"inbox detail:\n{traceback.format_exc()}")
            backoff = await _reconnect_sleep("inbox", e, backoff)


# --------------------------------------------------------------------- outbound reply tool


@_server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="reply",
            description=(
                "Send your result back to the controller over this channel. Pass the EXACT "
                "message_id from the channel tag and your full response. This is your only "
                "reply path — always call it when a channel task is done. If this reply will "
                "be relayed to a Teams chat, format it per the Teams conventions in the server "
                "instructions (pipe-tables row-per-line, Markdown links for all refs, no emojis)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message_id attribute from the <channel> tag",
                    },
                    "response": {
                        "type": "string",
                        "description": "Your result/answer text (or JSON-encoded structured data)",
                    },
                },
                "required": ["message_id", "response"],
            },
        ),
        types.Tool(
            name="send_teams",
            description=(
                "Post a message to a Microsoft Teams chat via the eCA hub. `text` is the "
                "message; `target` is the destination chat name (omit to post back to the chat "
                "that sent you the current task). The hub posts only for admin-triggered tasks "
                "and resolves the chat name; this returns whether it was delivered. Format per "
                "the Teams conventions in the server instructions before sending: pipe-tables "
                "with each row on its own line; every JIRA key / PR / commit / URL as a Markdown "
                "link (no bare refs); no emojis."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message to post to Teams"},
                    "target": {
                        "type": "string",
                        "description": "Destination chat name; omit for the originating chat",
                    },
                },
                "required": ["text"],
            },
        ),
        types.Tool(
            name="list_sessions",
            description=(
                "List the operator's OTHER live Claude Code sessions across all their machines "
                "(via the eCA hub): machine, repo, branch, status (what each is working on), and "
                "whether it is channel-push capable. Use to answer 'what is everyone working on?' "
                "or to find a target's machine.repo before send_to_session. Takes no arguments."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="send_to_session",
            description=(
                "Send a message to another of the operator's live sessions, relayed by the eCA "
                "hub, to keep work in sync. `target` is `machine.repo` (from list_sessions) or "
                "'all' to broadcast to every other live session. Default is fire-and-forget (an "
                "FYI); set `wait_for_reply` true (single target only) to block for the other "
                "session's answer. The message arrives at the target as a normal turn and does "
                "NOT grant it elevated tool permissions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Destination 'machine.repo' (from list_sessions), or 'all'",
                    },
                    "text": {"type": "string", "description": "Message to deliver"},
                    "wait_for_reply": {
                        "type": "boolean",
                        "description": "Block for the target's reply (single target only)",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Max seconds to wait when wait_for_reply (capped)",
                    },
                },
                "required": ["target", "text"],
            },
        ),
        types.Tool(
            name="check_session_message",
            description=(
                "Collect a reply to an earlier send_to_session(wait_for_reply=true) that didn't "
                "answer within the wait budget. Pass the `message_id` it returned. Returns the "
                "reply if it has landed, or {ready:false} to check again. (The reply is also "
                "pushed to you automatically when it lands; this is the explicit pull.)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message_id from the earlier send_to_session result",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Max seconds to wait for the reply this poll (capped)",
                    },
                },
                "required": ["message_id"],
            },
        ),
    ]


async def _mesh_reply(cfg: ChannelConfig, message_id: str, response: str) -> bool:
    try:
        async with _make_client(cfg, timeout=30.0) as c:
            res = await c.call_tool("reply", {"message_id": message_id, "response": response})
        return bool(_result_data(res).get("success"))
    except Exception as e:
        _log(f"mesh reply for {message_id} failed: {e}")
        return False


async def _mesh_send_teams(
    cfg: ChannelConfig, text: str, target: str | None, metadata: dict[str, Any]
) -> dict[str, Any]:
    """request_teams_send -> await_teams_send on the LOCAL server. Returns {ok, detail|error}.
    The hub decides whether to actually post (honors metadata.triggering_admin)."""
    args: dict[str, Any] = {
        "text": text,
        "metadata": metadata,
        "requester_session": cfg.identity,
    }
    if target:
        args["target"] = target
    try:
        async with _make_client(cfg, timeout=cfg.decision_timeout + 30.0) as c:
            res = await c.call_tool("request_teams_send", args)
            data = _result_data(res)
            if not data.get("success"):
                return {"ok": False, "error": "request_teams_send rejected by the hub server"}
            request_id = data.get("request_id")
            if not request_id:
                return {"ok": False, "error": "request_teams_send returned no request_id"}
            res2 = await c.call_tool(
                "await_teams_send", {"request_id": request_id, "timeout": cfg.decision_timeout}
            )
            d = _result_data(res2)
            if d.get("ready") and isinstance(d.get("request"), dict):
                req = d["request"]
                return {"ok": bool(req.get("ok")), "detail": req.get("detail")}
            return {"ok": False, "error": "no result within budget (the hub may still post it)"}
    except Exception as e:
        _log(f"send_teams failed: {e}")
        return {"ok": False, "error": str(e)}


async def _handle_reply(cfg: ChannelConfig, arguments: dict[str, Any]) -> list[types.TextContent]:
    assert _RT is not None
    message_id = str(arguments.get("message_id") or "")
    # `response` is the schema field; accept `text` as a tolerant alias ONLY when response is
    # absent (not merely empty) so a deliberately-empty response isn't replaced by a stray arg.
    raw = arguments.get("response")
    if raw is None:
        raw = arguments.get("text")
    response = str(raw if raw is not None else "")
    ok = await _mesh_reply(cfg, message_id, response)
    # Unblock the inbox loop so it claims the next message (only if this reply is for the
    # in-flight turn; a stale/duplicate reply still relays to the controller but doesn't advance).
    if _RT.inflight is not None and str(_RT.inflight.get("id")) == message_id:
        _RT.reply_event.set()
    if ok:
        _log(f"replied to controller for message {message_id}")
        return [types.TextContent(type="text", text="delivered to controller")]
    return [
        types.TextContent(
            type="text",
            text=(
                "WARNING: reply NOT recorded (unknown/already-finalized message_id). "
                "Check the message_id from the channel tag."
            ),
        )
    ]


async def _handle_send_teams(
    cfg: ChannelConfig, arguments: dict[str, Any]
) -> list[types.TextContent]:
    assert _RT is not None
    text = str(arguments.get("text") or "")
    target = arguments.get("target")
    target = str(target).strip() if target else None
    # Stamp the hub-trusted context. Two trusted origins:
    #   * NO in-flight task -> the OPERATOR is driving this session directly (their own action on
    #     their own machine), trusted exactly as the channel already treats the operator's local
    #     turns (see _handle_permission's inflight-None branch). Stamp operator_direct.
    #   * an in-flight task -> only an admin-triggered task ADDRESSED to this identity carries
    #     triggering_admin (mirrors the permission relay's auto-allow gate). A non-admin pushed
    #     task carries neither flag, so the hub refuses it — fail safe.
    inflight = _RT.inflight if isinstance(_RT.inflight, dict) else None
    if inflight is None:
        metadata: dict[str, Any] = {"operator_direct": True}
    else:
        in_meta = inflight.get("metadata") if isinstance(inflight.get("metadata"), dict) else {}
        addressed = inflight.get("recipient_session") == cfg.identity
        metadata = {
            "triggering_admin": bool(in_meta.get("triggering_admin")) and addressed,
            "conversation_id": in_meta.get("conversation_id"),
            "origin_message_id": inflight.get("id"),
        }
    if not text.strip():
        return [types.TextContent(type="text", text="send_teams: `text` is required")]
    result = await _mesh_send_teams(cfg, text, target, metadata)
    if result.get("ok"):
        where = target or "the originating chat"
        return [types.TextContent(type="text", text=f"posted to Teams ({where})")]
    detail = result.get("detail") or result.get("error") or "unknown error"
    return [types.TextContent(type="text", text=f"NOT posted to Teams: {detail}")]


def _fmt(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        return str(obj)


# Session/hub wait-budget contract (ADR-0015). The CALLER picks W; the hub (brain) waits W
# before it completes the op; this sidecar awaits W + margin so the await ALWAYS outlives the
# hub's wait — otherwise a slow/absent target makes the await expire first and surface a false
# "no result within budget" while the hub is still waiting (and will deliver). W is clamped here
# (the single source of truth) and sent in the payload, so the hub's wait and this await can't
# drift. The cap MUST NOT exceed the hub's own mesh-wait cap (brain MESH_WAIT_CAP_S = 240) — a
# larger W would be silently shortened by the hub, so it would not actually wait the W we sent.
# W (<=240) + margin (30) = 270 stays under the mesh 300s await cap.
_RELAY_AWAIT_MARGIN = 30.0
_RELAY_WAIT_CAP = 240.0
_RELAY_SEND_DEFAULT_WAIT = 120.0
_RELAY_CHECK_DEFAULT_WAIT = 60.0


def _relay_budget(wait_seconds: Any, default: float) -> float:
    """Clamp a caller-supplied wait to [1, _RELAY_WAIT_CAP]; None/non-positive/garbage -> default
    (so wait_for_reply with wait_seconds=0 means 'use the default', not a 0s no-wait)."""
    try:
        w = float(wait_seconds) if wait_seconds is not None else default
    except (TypeError, ValueError):
        w = default
    if w <= 0:
        w = default
    return min(max(w, 1.0), _RELAY_WAIT_CAP)


async def _mesh_session_op(
    cfg: ChannelConfig, op: str, payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    """request_session_op -> await_session_op on the LOCAL server. Returns {ok, result|error}.
    The hub (brain SessionRelayWatcher) does the cross-peer routing and completes the op — this
    just queues it locally and waits for the result."""
    try:
        async with _make_client(cfg, timeout=timeout + 30.0) as c:
            res = await c.call_tool(
                "request_session_op",
                {"op": op, "payload": payload, "requester_session": cfg.identity},
            )
            data = _result_data(res)
            if not data.get("success"):
                err = data.get("error")
                detail = err.get("message") if isinstance(err, dict) else err
                return {"ok": False, "error": f"request_session_op rejected: {detail or 'unknown'}"}
            request_id = data.get("request_id")
            if not request_id:
                return {"ok": False, "error": "request_session_op returned no request_id"}
            res2 = await c.call_tool(
                "await_session_op", {"request_id": request_id, "timeout": timeout}
            )
            d = _result_data(res2)
            if d.get("ready") and isinstance(d.get("request"), dict):
                req = d["request"]
                return {"ok": bool(req.get("ok")), "result": req.get("result")}
            return {"ok": False, "error": "no result within budget (the hub may still be working)"}
    except Exception as e:
        _log(f"session_op {op} failed: {e}")
        return {"ok": False, "error": str(e)}


def _session_op_error(out: dict[str, Any], label: str) -> str | None:
    """The hub's failure message for a _mesh_session_op result, or None on success."""
    if out.get("ok"):
        return None
    result = out.get("result")
    err = (result or {}).get("error") if isinstance(result, dict) else out.get("error")
    return f"{label}: {err}"


def _session_op_reply(out: dict[str, Any], label: str) -> list[types.TextContent]:
    """Full render for send/check: the hub's error, else the formatted result JSON."""
    if (err := _session_op_error(out, label)) is not None:
        return [types.TextContent(type="text", text=err)]
    result = out.get("result") if isinstance(out.get("result"), dict) else {}
    return [types.TextContent(type="text", text=_fmt(result))]


async def _handle_list_sessions(
    cfg: ChannelConfig, arguments: dict[str, Any]
) -> list[types.TextContent]:
    out = await _mesh_session_op(cfg, "list", {}, timeout=60.0)
    if (err := _session_op_error(out, "could not list sessions")) is not None:
        return [types.TextContent(type="text", text=err)]
    result = out.get("result") if isinstance(out.get("result"), dict) else {}
    sessions = result.get("sessions") or []
    if not sessions:
        note = "no other live sessions are currently running"
        unreachable = result.get("unreachable_machines") or []
        if unreachable:
            note += f"\nunreachable machines: {_fmt(unreachable)}"
        return [types.TextContent(type="text", text=note)]
    return [types.TextContent(type="text", text=_fmt(result))]


async def _handle_send_to_session(
    cfg: ChannelConfig, arguments: dict[str, Any]
) -> list[types.TextContent]:
    target = str(arguments.get("target") or "").strip()
    text = str(arguments.get("text") or "")
    wait_for_reply = bool(arguments.get("wait_for_reply"))
    wait_seconds = arguments.get("wait_seconds")
    if not target:
        return [
            types.TextContent(
                type="text", text="send_to_session: `target` is required (machine.repo or 'all')"
            )
        ]
    if not text.strip():
        return [types.TextContent(type="text", text="send_to_session: `text` is required")]
    if wait_for_reply:
        # Clamp W once and send it in the payload (the hub waits exactly this); await W + margin.
        w = _relay_budget(wait_seconds, _RELAY_SEND_DEFAULT_WAIT)
        payload: dict[str, Any] = {
            "target": target, "text": text, "wait_for_reply": True, "wait_seconds": w,
        }
        await_timeout = min(w + _RELAY_AWAIT_MARGIN, 300.0)
    else:
        payload = {"target": target, "text": text, "wait_for_reply": False}
        await_timeout = 60.0  # notify completes fast (the hub just injects + acks)
    out = await _mesh_session_op(cfg, "send", payload, timeout=await_timeout)
    return _session_op_reply(out, "send_to_session failed")


async def _handle_check_session_message(
    cfg: ChannelConfig, arguments: dict[str, Any]
) -> list[types.TextContent]:
    message_id = str(arguments.get("message_id") or "").strip()
    if not message_id:
        return [
            types.TextContent(
                type="text", text="check_session_message: `message_id` is required"
            )
        ]
    # Same budget contract as send: the hub polls W, we await W + margin (so the await outlives
    # the hub's poll instead of the previous fixed 60s < hub's poll mismatch).
    w = _relay_budget(arguments.get("wait_seconds"), _RELAY_CHECK_DEFAULT_WAIT)
    out = await _mesh_session_op(
        cfg,
        "check",
        {"message_id": message_id, "wait_seconds": w},
        timeout=min(w + _RELAY_AWAIT_MARGIN, 300.0),
    )
    return _session_op_reply(out, "check_session_message failed")


@_server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    assert _RT is not None
    cfg = _RT.cfg
    if name == "reply":
        return await _handle_reply(cfg, arguments)
    if name == "send_teams":
        return await _handle_send_teams(cfg, arguments)
    if name == "list_sessions":
        return await _handle_list_sessions(cfg, arguments)
    if name == "send_to_session":
        return await _handle_send_to_session(cfg, arguments)
    if name == "check_session_message":
        return await _handle_check_session_message(cfg, arguments)
    raise ValueError(f"unknown tool: {name}")


# --------------------------------------------------------------------- permission relay + routing


def _is_auto_pass(tool_name: str, cfg: ChannelConfig) -> bool:
    """Tools allowed without any approval round-trip, even on a non-admin turn: our own
    delivery/control tools (reply, send_teams, list_sessions, send_to_session — the hub applies
    any policy on its side) and the configured read-only set."""
    if tool_name in OUR_TOOLS:
        return True
    return tool_name in cfg.auto_pass_tools


async def _send_permission(write_stream: Any, request_id: Any, behavior: str) -> None:
    notif = JSONRPCNotification(
        jsonrpc="2.0",
        method=PERM_REPLY,
        params={"request_id": request_id, "behavior": behavior},
    )
    await write_stream.send(SessionMessage(message=JSONRPCMessage(notif)))


async def _route_approval(
    cfg: ChannelConfig, inflight: dict[str, Any], tool_name: str, description: str, preview: str
) -> str:
    """Non-admin path: open a Phase-3 approval on the LOCAL server, wait for the brain's
    verdict (its ApprovalWatcher DMs Jeremy). Returns 'allow' or 'deny'; DEFAULT-DENY on any
    failure or timeout — we NEVER auto-approve a non-admin turn."""
    try:
        async with _make_client(cfg, timeout=cfg.decision_timeout + 30.0) as c:
            res = await c.call_tool(
                "request_approval",
                {
                    "session_id": cfg.identity,
                    "tool_name": tool_name,
                    "tool_input": {
                        "description": description,
                        "input_preview": preview,
                        "sender": inflight.get("sender"),
                        "message_id": inflight.get("id"),
                    },
                },
            )
            data = _result_data(res)
            if not data.get("success"):
                _log(f"request_approval failed: {data}; default-deny")
                return "deny"
            approval_id = data.get("approval_id")
            if not approval_id:
                # success without an id is a server contract violation — fail safe, don't
                # call await_decision with a None id (which could match the wrong/first pending).
                _log(f"request_approval ok but no approval_id ({data}); default-deny")
                return "deny"
            res2 = await c.call_tool(
                "await_decision", {"approval_id": approval_id, "timeout": cfg.decision_timeout}
            )
            d = _result_data(res2)
            if d.get("ready") and isinstance(d.get("approval"), dict):
                return "allow" if d["approval"].get("decision") == "allow" else "deny"
            _log(f"approval {approval_id} not decided within budget; default-deny")
            return "deny"
    except Exception as e:
        _log(f"approval routing error: {e}; default-deny")
        return "deny"


async def _handle_permission(write_stream: Any, cfg: ChannelConfig, params: dict[str, Any]) -> None:
    request_id = params.get("request_id") or params.get("requestId") or params.get("id")
    tool_name = str(params.get("tool_name") or "?")
    description = str(params.get("description") or "")
    preview = str(params.get("input_preview") or "")

    # Our own tools are delivery/control paths — always allow the tool CALL, regardless of who
    # triggered (the hub re-applies policy: send_teams on triggering_admin; session relay routing).
    if tool_name in OUR_TOOLS:
        await _send_permission(write_stream, request_id, "allow")
        return

    assert _RT is not None
    inflight = _RT.inflight
    if inflight is None:
        # No channel turn in flight => this is the operator's OWN local work. Stay silent;
        # the local terminal dialog (which is always also open) handles it.
        _log(f"permission_request {request_id} ({tool_name}) — no in-flight channel turn; "
             "leaving to the local terminal dialog")
        return

    # Admin auto-allow: trust the brain's per-batch `triggering_admin` stamp (the brain is the
    # authorization authority — invariant 8 / ADR-0006 — exactly as the launcher trusts the
    # brain's dispatch). The mesh bearer + loopback-bind + SSH tunnel (ADR-0011) are the trust
    # boundary; the co-driving operator watching every turn in their terminal is the backstop.
    # Defense-in-depth: only honor the stamp on a message EXPLICITLY addressed to this identity,
    # so a blind broadcast (recipient_session=NULL) can't carry triggering_admin into an auto-allow.
    meta = inflight.get("metadata") if isinstance(inflight.get("metadata"), dict) else {}
    addressed = inflight.get("recipient_session") == cfg.identity
    if meta.get("triggering_admin") is True and addressed:
        await _send_permission(write_stream, request_id, "allow")
        _log(f"auto-allowed {tool_name} (admin turn, msg {inflight.get('id')})")
        return

    if _is_auto_pass(tool_name, cfg):
        await _send_permission(write_stream, request_id, "allow")
        _log(f"auto-passed read-only {tool_name} (non-admin turn)")
        return

    decision = await _route_approval(cfg, inflight, tool_name, description, preview)
    await _send_permission(write_stream, request_id, decision)
    _log(f"routed {tool_name} -> {decision} (non-admin turn, msg {inflight.get('id')})")


async def _tee_reader(
    read_stream: Any, dst: Any, write_stream: Any, cfg: ChannelConfig, tg: Any
) -> None:
    """Sniff raw inbound; intercept the permission notification (the typed loop would drop it),
    forward everything else to Server.run()."""
    try:
        async for msg in read_stream:
            try:
                root = getattr(getattr(msg, "message", None), "root", None)
                method = getattr(root, "method", None)
                if method == INITIALIZED and _RT is not None:
                    _RT.initialized.set()
                elif method == PERM_REQUEST:
                    params = getattr(root, "params", None) or {}
                    # Handle out-of-band so the read loop keeps forwarding (await_decision blocks).
                    tg.start_soon(_handle_permission, write_stream, cfg, params)
                    continue  # consumed; do NOT forward (the typed loop would drop+warn)
            except Exception as e:
                _log(f"tee inspect error: {e}")
            await dst.send(msg)
    finally:
        await dst.aclose()


# --------------------------------------------------------------------- serve


async def _serve(cfg: ChannelConfig) -> None:
    global _RT
    _RT = _Runtime(cfg=cfg)
    init_options = _server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={"claude/channel": {}, "claude/channel/permission": {}},
    )
    if not cfg.enabled:
        # SAFETY: wired-but-disabled. Complete the MCP handshake so Claude Code is happy, but
        # DO NOT start any loop — no polling, no inbox claims, no push, no relay. A disabled
        # adapter that polled would claim messages and push them into a channel nobody routes
        # (silent message loss); leaving it inert lets session.py notify+pull own the inbox.
        _log("channel disabled (CHANNEL_ENABLED=false); inert handshake only.")
        async with stdio_server() as (read_stream, write_stream):
            await _server.run(read_stream, write_stream, init_options)
        return

    _log(f"starting channel sidecar (identity={cfg.identity!r}, local={cfg.local_url})")
    async with stdio_server() as (read_stream, write_stream):
        tee_send, tee_recv = anyio.create_memory_object_stream(256)
        async with anyio.create_task_group() as tg:
            tg.start_soon(_presence_loop, cfg)
            tg.start_soon(_inbox_loop, cfg, write_stream)
            tg.start_soon(_tee_reader, read_stream, tee_send, write_stream, cfg, tg)
            # The server loop owns the protocol (handshake, ping, shutdown when stdin closes).
            await _server.run(tee_recv, write_stream, init_options)
            tg.cancel_scope.cancel()


def main() -> None:
    cfg = _resolve_config(sys.argv[1:])
    try:
        anyio.run(_serve, cfg)
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    main()
