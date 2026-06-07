"""fast-mcp-claude-session — live-session presence + inbox-notify sidecar (Phase 4).

The launch wrapper (start-session.sh) starts ONE of these per interactive Claude Code
dev session, in the background, sharing the operator's trusted env (it legitimately
holds the mesh bearer — unlike a launcher-spawned headless worker, whose env is
scrubbed). Its lifetime is tied to the session: it watches its parent pid and exits
when the session process dies (reparented to init).

It does two things against the LOCAL fast-mcp-claude HTTP server, on one connection:

  1. UP-REPORTING (presence). It is the SOLE announcer of this session's identity,
     heartbeating ``announce(identity, summary, metadata)`` every ``heartbeat`` seconds
     with role="live-session" and a status summary it reads from a local JSON status
     file the CC hooks (fast-mcp-claude-session-hook) write. Routing hook status through
     the single announcer is deliberate: announce() is a full upsert (no merge), so two
     processes announcing one identity would clobber each other's metadata.

  2. DOWN-DELIVERY notify (NOT push). Channels were the v2 auto-push path, but Claude
     Code 2.1.x removed the ``--dangerously-load-development-channels`` dev-server load
     path, so there is no way to inject a prompt into a live session. Instead the sidecar
     polls ``list_messages(status="queued")``, filters to messages addressed to THIS
     session, and on a newly-arrived one fires a macOS notification + writes an unread
     count to a badge file (for the statusline). It NEVER claims (pop) the message — that
     stays for the operator's ``/fleet-inbox`` pull (wait_for_instruction -> reply), which
     is the delivery + reply source of truth.

Strict opt-in: arms only with ``--enabled`` / SESSION_ENABLED. It never spawns anything.

Config (CLI flag, else env, else Settings/.env default):
    --enabled/--no-enabled / SESSION_ENABLED   arm the heartbeat + inbox watch (off)
    --identity   / CRM_IDENTITY      presence + mailbox identity (default peer_name)
    --local-url  / CRM_LOCAL_URL     local server MCP URL (default http://127.0.0.1:<port>/mcp)
                   MCP_API_KEY        bearer for the local server
    --status-file/ CRM_SESSION_STATUS_FILE   JSON status file the hooks write
    --badge-file / CRM_SESSION_BADGE_FILE     unread-count file for the statusline
    --summary    / CRM_SUMMARY       fallback presence blurb if the status file has none
    --poll       / CRM_SESSION_POLL_S         inbox poll seconds (default 10)
    --heartbeat  / CRM_SESSION_HEARTBEAT_S    presence heartbeat seconds (default 15)
    --notify/--no-notify / SESSION_NOTIFY     fire macOS notifications (default on)
    --parent-pid                     watch this pid; exit when it dies (default getppid())
                   CRM_SESSION_DEBUG set to "0" to silence stderr diagnostics
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

SENDER_RE_NOTE = "identity must match the server SESSION_RE ^[a-zA-Z0-9_.-]{1,128}$"


def _log(msg: str) -> None:
    if os.environ.get("CRM_SESSION_DEBUG", "1") != "0":
        print(f"[fast-mcp-claude-session] {msg}", file=sys.stderr, flush=True)


@dataclass
class SessionConfig:
    identity: str
    local_url: str
    api_key: str | None
    status_file: str | None
    badge_file: str | None
    summary: str | None
    poll: float
    heartbeat: float
    enabled: bool
    notify: bool
    parent_pid: int


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


def _resolve_config(argv: list[str]) -> SessionConfig:
    p = argparse.ArgumentParser(prog="fast-mcp-claude-session")
    p.add_argument("--identity", default=None)
    p.add_argument("--local-url", default=None)
    p.add_argument("--status-file", default=None)
    p.add_argument("--badge-file", default=None)
    p.add_argument("--summary", default=None)
    p.add_argument("--poll", type=float, default=None)
    p.add_argument("--heartbeat", type=float, default=None)
    p.add_argument("--parent-pid", type=int, default=None)
    p.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--notify", action=argparse.BooleanOptionalAction, default=None)
    args = p.parse_args(argv)

    peer_name, port, api_key = "default", 5473, None
    poll, heartbeat = 10.0, 15.0
    enabled, notify, status_file = False, True, None
    try:
        from .config import get_settings

        s = get_settings()
        peer_name = s.peer_name or peer_name
        port = s.mcp_port
        api_key = s.mcp_api_key
        poll = float(s.session_poll_s)
        heartbeat = float(s.session_heartbeat_s)
        enabled = s.session_enabled
        notify = s.session_notify
        status_file = s.session_status_file or None
    except Exception as e:  # bad/missing .env must not kill the sidecar
        _log(f"settings unavailable, using bare defaults: {e}")

    identity = args.identity or os.environ.get("CRM_IDENTITY") or peer_name
    local_url = (
        args.local_url or os.environ.get("CRM_LOCAL_URL") or f"http://127.0.0.1:{port}/mcp"
    )
    api_key = os.environ.get("MCP_API_KEY", api_key)
    status_file = (
        args.status_file or os.environ.get("CRM_SESSION_STATUS_FILE") or status_file
    )
    badge_file = args.badge_file or os.environ.get("CRM_SESSION_BADGE_FILE")
    summary = args.summary or os.environ.get("CRM_SUMMARY")
    poll = args.poll if args.poll is not None else _env_float("CRM_SESSION_POLL_S", poll)
    heartbeat = (
        args.heartbeat
        if args.heartbeat is not None
        else _env_float("CRM_SESSION_HEARTBEAT_S", heartbeat)
    )
    enabled = (
        args.enabled if args.enabled is not None else _env_bool("SESSION_ENABLED", enabled)
    )
    notify = args.notify if args.notify is not None else _env_bool("SESSION_NOTIFY", notify)
    parent_pid = args.parent_pid if args.parent_pid is not None else os.getppid()
    return SessionConfig(
        identity=identity,
        local_url=local_url,
        api_key=api_key,
        status_file=status_file,
        badge_file=badge_file,
        summary=summary,
        poll=poll,
        heartbeat=heartbeat,
        enabled=enabled,
        notify=notify,
        parent_pid=parent_pid,
    )


def _result_data(result: Any) -> dict[str, Any]:
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


def _read_status(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _build_presence(cfg: SessionConfig) -> tuple[str | None, dict[str, Any]]:
    """Summary + metadata for announce(), from the hook-written status file."""
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
        "machine": st.get("machine"),
        "repo": repo or None,
        "cwd": st.get("cwd"),
        "branch": st.get("branch"),
        "status": status,
        "last": last or None,
        "session_pid": cfg.parent_pid,
        "status_updated_at": st.get("updated_at"),
    }
    return summary, {k: v for k, v in meta.items() if v is not None}


def _notify_macos(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    # osascript args are passed as a list (no shell), so message text is not interpolated
    # into a shell command — but quotes inside an AppleScript string still need escaping.
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')[:240]
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')[:80]
    script = f'display notification "{safe_msg}" with title "{safe_title}" sound name "Glass"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception as e:  # notifications are best-effort
        _log(f"notify failed (continuing): {e}")


def _write_badge(path: str | None, count: int) -> None:
    if not path:
        return
    try:
        tmp = os.path.expanduser(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(count))
        os.replace(tmp, os.path.expanduser(path))
    except OSError as e:
        _log(f"badge write failed (continuing): {e}")


@dataclass
class _Watch:
    seen: set[str] = field(default_factory=set)


async def _bridge(cfg: SessionConfig) -> None:
    """Single connection: heartbeat presence + watch inbox -> notify (never claim)."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    def _make_client() -> Client:
        if cfg.api_key:
            transport = StreamableHttpTransport(
                cfg.local_url, headers={"Authorization": f"Bearer {cfg.api_key}"}
            )
            return Client(transport)
        return Client(cfg.local_url)

    watch = _Watch()
    backoff = 1.0
    last_announce = 0.0
    bridge_start = time.monotonic()
    warned = {"announce": False, "stale": False}
    _AUTH_HINTS = ("401", "403", "unauthorized", "forbidden")
    while True:
        if _parent_dead(cfg.parent_pid):
            _log("parent session gone; exiting")
            return
        try:
            async with _make_client() as c:
                backoff = 1.0
                _log(f"connected to {cfg.local_url} as identity={cfg.identity!r}")
                while True:
                    if _parent_dead(cfg.parent_pid):
                        _log("parent session gone; exiting")
                        return
                    now = time.time()
                    if now - last_announce >= cfg.heartbeat:
                        summary, meta = _build_presence(cfg)
                        # If status is still the wrapper's "starting" seed well past launch,
                        # the up-reporting hooks never fired (e.g. --settings schema drift on
                        # this CC version) - announce that honestly instead of stale presence.
                        grace = max(2 * cfg.heartbeat, 20.0)
                        stale = time.monotonic() - bridge_start > grace
                        if meta.get("status") == "starting" and stale:
                            meta["status"] = "presence-unverified"
                            summary = f"{summary} [up-reporting hooks not firing]"
                            if not warned["stale"]:
                                _log("up-reporting hooks never updated status; presence-"
                                     "unverified (check the --settings hook schema for this CC)")
                                warned["stale"] = True
                        try:
                            res = await c.call_tool(
                                "announce",
                                {
                                    "identity": cfg.identity,
                                    "summary": summary,
                                    "metadata": meta,
                                },
                            )
                            data = _result_data(res)
                            if not data.get("success") and not warned["announce"]:
                                _log(f"announce REJECTED for identity={cfg.identity!r} "
                                     f"(invalid? this session is INVISIBLE to the brain): {data}")
                                warned["announce"] = True
                        except Exception as e:
                            _log(f"announce failed (continuing): {e}")
                        last_announce = now
                    await _check_inbox(c, cfg, watch)
                    await asyncio.sleep(cfg.poll)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            msg = str(e).lower()
            if any(h in msg for h in _AUTH_HINTS):
                # Never retry-storm: 5 bad bearers lock the WHOLE endpoint for 60s.
                _log(f"auth error: {e}; cooling down 90s")
                await asyncio.sleep(90.0)
            else:
                _log(f"bridge error: {e}; reconnecting in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


async def _check_inbox(c: Any, cfg: SessionConfig, watch: _Watch) -> None:
    """List queued messages for THIS identity (no claim) and notify on new arrivals."""
    try:
        # Filter server-side by recipient_session (index-backed) so a busy hub (>200 globally
        # queued) can't push THIS session's messages out of the newest-N window and lose a
        # notification. The client-side check below stays as belt-and-braces.
        res = await c.call_tool(
            "list_messages",
            {"status": "queued", "limit": 200, "recipient_session": cfg.identity},
        )
    except Exception as e:
        _log(f"list_messages failed (continuing): {e}")
        return
    data = _result_data(res)
    if not data.get("success"):
        return
    mine = [
        m
        for m in (data.get("messages") or [])
        if isinstance(m, dict) and m.get("recipient_session") == cfg.identity
    ]
    ids = {str(m.get("id")) for m in mine}
    _write_badge(cfg.badge_file, len(mine))
    new = [m for m in mine if str(m.get("id")) not in watch.seen]
    watch.seen = ids  # drop ids that left the queue (claimed/replied/expired)
    for m in new:
        sender = m.get("sender") or "evolv-coder-agent"
        preview = (m.get("prompt") or "").strip().replace("\n", " ")[:120]
        _log(f"new inbox message {m.get('id')} from {sender}: {preview!r}")
        if cfg.notify:
            _notify_macos(
                title=f"eCA fleet: {sender}",
                message=f"{preview}  —  run /fleet-inbox {cfg.identity}",
            )


def _parent_dead(parent_pid: int) -> bool:
    """True once the session process is gone (reparented to init / not our parent)."""
    if parent_pid <= 1:
        return False
    return os.getppid() != parent_pid


def main(argv: list[str] | None = None) -> None:
    cfg = _resolve_config(argv if argv is not None else sys.argv[1:])
    if not cfg.enabled:
        _log("disabled (set --enabled / SESSION_ENABLED=true to arm); exiting")
        return
    _log(
        f"starting (identity={cfg.identity!r}, poll={cfg.poll}s, heartbeat={cfg.heartbeat}s, "
        f"notify={cfg.notify}, parent_pid={cfg.parent_pid}); {SENDER_RE_NOTE}"
    )
    try:
        asyncio.run(_bridge(cfg))
    except KeyboardInterrupt:
        pass
    finally:
        _write_badge(cfg.badge_file, 0)
    _log("stopped")


if __name__ == "__main__":
    main()
