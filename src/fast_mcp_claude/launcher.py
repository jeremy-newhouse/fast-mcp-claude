"""fast-mcp-claude-launcher — turn this machine into a spawn target for the fleet.

A long-running asyncio process (pm2-managed, NOT an MCP server) that long-polls the
LOCAL fast-mcp-claude HTTP server's inbox for tasks addressed to this launcher's
identity (f"{peer_name}_launcher"), then spawns each as a headless `claude -p` run in
an allowlisted working directory with a tools ceiling, and posts the result back via
the existing `reply` tool. It mirrors channel.py's structure (fastmcp Client to the
LOCAL server, reconnect-with-backoff, announce heartbeat, CLI>env>Settings precedence,
strict opt-in inertness) but the leg is *exec* instead of *push*.

Why a separate process (the evolv-coder-agent daemon lesson): the server stays zero-exec —
no tool ever spawns a subprocess. All execution lives here, behind a strict opt-in,
an allowlisted cwd, and a tools ceiling, so the MCP server's network surface keeps no
RCE primitive.

Correctness invariants (do not "simplify" away):
  * STRICT OPT-IN. When disabled (the default) OR when the claude binary is missing,
    the launcher logs once and idle-sleeps forever — it NEVER polls and NEVER claims.
    A claim flips a message to 'delivered'; claiming work we can't run would lose it.
  * CONCURRENCY SLOT BEFORE CLAIM. We acquire a semaphore slot *before* calling
    wait_for_instruction, so we never claim a task we have no capacity to run.
  * STALE-CLAIM REAPER. At startup (before the poll loop), any 'delivered' row
    addressed to our identity — orphaned by a previous crash mid-task — is replied
    fail-fast (launcher_restarted_task_lost) so the controller doesn't hang to TTL.
  * OWNER-TOKEN GATE (ECA-71/ADR-0029). The reaper and the poll loop never run until this
    process's OWN announce on the CURRENT connection is confirmed successful; a duplicate
    instance whose announce is refused (IDENTITY_LIVE_ELSEWHERE) never reaps the real
    owner's in-flight tasks and never competes for new mailbox work (see
    `_wait_for_owner_confirmed_or_reconnect`).
  * ALWAYS-REPLY. Every claimed message gets EXACTLY ONE reply() on EVERY exit path
    (success, nonzero exit, timeout-kill, spawn failure, envelope rejection, internal
    exception, launcher shutdown, client reconnect). The whole task handler is wrapped so
    any unexpected exception still posts a minimal launcher_internal reply, and replies are
    routed through a `_ClientBox` so a mid-task reconnect or a shutdown-time cancellation
    can't strand a reply on a connection that has already closed.
  * REPLY FITS. The JSON-encoded reply is pre-truncated (head+tail) to stay under
    launcher_reply_max_bytes (<< the server's 4 MB validate_response cap); a reply
    above 4 MB is rejected and the controller hangs to TTL.
  * SCRUBBED ENV. The spawned worker does NOT inherit MCP_API_KEY or CRM_* vars (the
    mesh bearer); it keeps HOME/PATH/etc. Tasks run in their own process group so a
    timeout can SIGTERM/SIGKILL the whole group.
  * BOUNDED OUTPUT. A task's stdout/stderr are streamed and capped (MAX_SUBPROCESS_OUTPUT_
    BYTES per stream, a rolling tail window — see `_read_capped`) as they're produced,
    never accumulated fully in memory; a runaway task's output can't OOM the process.
  * GROUP-WIDE KILL. On timeout, `_kill_group` verifies the WHOLE process group has
    exited (polling `_group_alive`, not just the leader) before considering the kill
    complete, force-SIGKILLing the group if a grandchild outlives the SIGTERM grace.
  * RELAY-READY GATE (Phase 3). When the approval hook is enabled, gated-task claiming
    blocks until the approval relay's unix socket is confirmed listening, and stops again
    if the relay ever dies mid-run — see `_run_approval_relay_supervised` and
    `_wait_for_relay_healthy_or_reconnect`. An unreachable relay makes the worker hook fall
    back to "ask", which does NOT override-deny a tool already covered by --allowedTools.

Config (CLI flag, else env, else Settings/.env default):
    --enabled/--no-enabled / LAUNCHER_ENABLED   arm the poll/spawn loop
                                     (default: Settings.launcher_enabled, off)
    --identity   / CRM_IDENTITY      mailbox + presence identity
                                     (default: f"{peer_name}_launcher")
    --local-url  / CRM_LOCAL_URL     local server MCP URL (default http://127.0.0.1:<port>/mcp)
                   MCP_API_KEY        bearer for the local server (if it requires auth)
    --poll       / CRM_POLL_S        long-poll seconds per wait_for_instruction (default 25)
    --heartbeat  / CRM_HEARTBEAT_S   presence heartbeat seconds (default 20)
                   CRM_LAUNCHER_DEBUG set to "0" to silence stderr diagnostics
"""

import argparse
import asyncio
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .utils.validation import SESSION_RE

# How long to wait, in seconds, for `claude --version` at startup before giving up.
VERSION_PROBE_TIMEOUT_S = 10.0
# Grace between SIGTERM and SIGKILL when killing a task's process group.
KILL_GRACE_S = 10.0
# Cadence for probing whether any process is still alive in a killed process group during
# the SIGTERM grace period. There is no asyncio primitive for "wait for a process GROUP"
# (only for our single direct child), so we poll group-wide existence instead.
_KILL_POLL_INTERVAL_S = 0.2
# Tail of stderr kept on the reply (bytes), before the global reply budget trims more.
STDERR_TAIL_BYTES = 8192
# Max bytes of a spawned task's stdout/stderr retained in memory at any time (a rolling
# TAIL window per stream) while the process is producing output. Bounds the launcher's
# memory regardless of how much a runaway/misbehaving task emits, instead of the previous
# proc.communicate(), which read each stream fully to EOF before anything was truncated.
MAX_SUBPROCESS_OUTPUT_BYTES = 4 * 1024 * 1024
# Grace period, once a subprocess has exited or been force-killed, for its bounded
# stdout/stderr reader tasks to observe pipe EOF and hand back their buffered tail.
_READ_DRAIN_TIMEOUT_S = 5.0
# Backoff for restarting the approval-relay server if it exits or crashes after startup.
_RELAY_RESTART_BACKOFF_S = 1.0
_RELAY_RESTART_BACKOFF_MAX_S = 30.0
# Env vars stripped from the spawned worker's environment (mesh bearer must not leak).
_SCRUB_ENV_EXACT = ("MCP_API_KEY",)
_SCRUB_ENV_PREFIX = ("CRM_",)
# Consecutive failed announces before the heartbeat tells the bridge to rebuild the client.
# A `fast-mcp-claude` restart kills our MCP session; the long-poll main loop can stay blocked
# against that dead session, so the heartbeat is the reliable detector. ~2 intervals avoids
# rebuilding on a one-off blip while still recovering in well under a minute (GH #3).
_ANNOUNCE_FAILS_BEFORE_RECONNECT = 2
# Auth-failure hints: a bad bearer won't be fixed by reconnecting (and 5 in a row lock the
# whole endpoint for 60s), so an auth error must NEVER trip a rebuild.
_AUTH_HINTS = ("401", "403", "unauthorized", "forbidden")
# Consecutive auth-shaped announce failures before the heartbeat gives up WAITING for owner
# confirmation (does NOT trip reconnect_needed -- that would reintroduce the lockout risk
# _AUTH_HINTS exists to avoid). A bad bearer never produces a well-formed IDENTITY_LIVE_ELSEWHERE
# refusal, so blocking the reap/poll gate on it forever would be a pure regression versus
# pre-ECA-71 behavior, which at least reached the poll loop and let its own error handling
# (reconnect-with-backoff) take over. Higher than _ANNOUNCE_FAILS_BEFORE_RECONNECT since giving
# up here is a one-way "proceed without confirmation" decision, not a cheap reconnect.
_AUTH_FAILS_BEFORE_GIVING_UP = 3


class _ReconnectNeeded(Exception):
    """Force a client rebuild from the bridge loop. Raised when the heartbeat detects the local
    server's MCP session has gone (a `fast-mcp-claude` restart surfaces ONLY as a persistent
    'announce failed' — which the blocked long-poll wouldn't otherwise act on, so the launcher
    would heartbeat 'Session terminated' forever and silently drop out of who()). Caught by the
    outer reconnect-with-backoff in _bridge (GH #3)."""


def _log(msg: str) -> None:
    # stderr only (pm2 captures it). Mirrors channel.py's _log.
    if os.environ.get("CRM_LAUNCHER_DEBUG", "1") != "0":
        print(f"[fast-mcp-claude-launcher] {msg}", file=sys.stderr, flush=True)


class _Counter:
    """A tiny mutable int the heartbeat loop can read while handlers inc/dec it
    (single asyncio loop, so no lock needed)."""

    def __init__(self) -> None:
        self.value = 0


@dataclass
class LauncherConfig:
    identity: str
    local_url: str
    api_key: str | None
    poll: float
    heartbeat: float
    enabled: bool
    # Spawn policy (resolved from Settings at startup).
    claude_bin: str
    cwd_allowlist: list[Path]
    tools_ceiling: list[str]
    max_concurrent: int
    task_timeout_s: float
    reply_max_bytes: int
    setting_sources: str
    # Local-server auth posture (NOT the worker's — used by the startup guard). If the
    # local mesh endpoint is unauthenticated, a spawned worker on localhost could spoof
    # reply/send_prompt, so we refuse to arm. Defaults to the safe (auth-on) posture so
    # a missing-Settings boot does not accidentally pass the guard.
    mcp_auth_enabled: bool
    mcp_api_key_present: bool
    # Phase 3 approval hook (defaulted so existing constructions stay valid). When armed,
    # _build_cmd injects a launcher-controlled --settings PreToolUse hook. approval_hook_cmd
    # is the absolute path to fast-mcp-claude-hook resolved ONCE at startup (None => the
    # hook is left disabled rather than injecting a broken command).
    approval_hook_enabled: bool = False
    approval_hook_cmd: str | None = None
    approval_decision_timeout_s: float = 300.0
    approval_auto_pass_tools: str = "Read,Glob,Grep"
    approval_hook_selftest: bool = True
    # Unix socket the worker hook talks to; the launcher relays approvals over it so the
    # worker never gets the mesh bearer. Path only (not a secret) appears in the worker argv.
    approval_socket_path: str = ""
    # ECA-71 / ADR-0029 owner-token: a per-process boot token stamped into every announce so the
    # server's duplicate-identity guard can refuse a second launcher reusing this identity. Blank
    # keeps pre-ECA-71 behavior (tokenless announces are never refused). Set once at startup.
    announce_token: str = ""


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


def _parse_tools_ceiling(raw: str) -> list[str]:
    """Split a comma-separated tool-spec ceiling, e.g. 'Read,Grep,Bash(uv run*)'.

    We split on commas only (tool specs can contain parens/spaces/globs but not
    commas), trim whitespace, and drop empties.
    """
    return [t.strip() for t in raw.split(",") if t.strip()]


def _resolve_config(argv: list[str]) -> LauncherConfig:
    p = argparse.ArgumentParser(prog="fast-mcp-claude-launcher")
    p.add_argument("--identity", default=None)
    p.add_argument("--local-url", default=None)
    p.add_argument("--poll", type=float, default=None)
    p.add_argument("--heartbeat", type=float, default=None)
    p.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Arm the poll/spawn loop. Default comes from LAUNCHER_ENABLED env, else "
            "Settings.launcher_enabled (off). When off the launcher idle-sleeps "
            "forever — no polling, no inbox claims, no spawning."
        ),
    )
    args = p.parse_args(argv)

    # Defaults from the project Settings/.env; the launcher and the server are
    # normally launched from the same directory with the same config.
    peer_name, port, api_key, poll, heartbeat = "default", 5473, None, 25.0, 20.0
    launcher_enabled = False
    claude_bin = "claude"
    cwd_allowlist: list[Path] = []
    tools_ceiling: list[str] = []
    max_concurrent = 2
    task_timeout_s = 900.0
    reply_max_bytes = 262144
    setting_sources = ""
    approval_hook_enabled = False
    approval_decision_timeout_s = 300.0
    approval_auto_pass_tools = "Read,Glob,Grep"
    approval_hook_selftest = True
    approval_socket_path = ""
    # Safe defaults for the auth guard: assume auth-ON so a missing-Settings boot does
    # NOT accidentally pass the unauthenticated-mesh guard and arm.
    mcp_auth_enabled = True
    try:
        from .config import get_settings

        s = get_settings()
        peer_name = s.peer_name or peer_name
        port = s.mcp_port
        api_key = s.mcp_api_key
        poll = float(s.poll_max_wait_s)
        heartbeat = float(s.poll_heartbeat_s)
        launcher_enabled = s.launcher_enabled
        claude_bin = s.launcher_claude_bin
        cwd_allowlist = s.launcher_cwd_allowlist_resolved
        tools_ceiling = _parse_tools_ceiling(s.launcher_tools_ceiling)
        max_concurrent = max(1, int(s.launcher_max_concurrent))
        task_timeout_s = float(s.launcher_task_timeout_s)
        reply_max_bytes = int(s.launcher_reply_max_bytes)
        setting_sources = s.launcher_setting_sources
        approval_hook_enabled = bool(s.launcher_approval_hook_enabled)
        approval_decision_timeout_s = float(s.launcher_approval_decision_timeout_s)
        approval_auto_pass_tools = s.launcher_approval_auto_pass_tools
        approval_hook_selftest = bool(s.launcher_approval_hook_selftest)
        approval_socket_path = s.launcher_approval_socket_path
        mcp_auth_enabled = bool(s.mcp_auth_enabled)
    except Exception as e:  # bad/missing .env shouldn't crash the launcher boot
        _log(f"settings unavailable, using bare defaults: {e}")

    # Precedence (highest first): CLI flag > env var > Settings default.
    identity = (
        args.identity or os.environ.get("CRM_IDENTITY") or f"{peer_name}_launcher"
    )
    local_url = args.local_url or os.environ.get("CRM_LOCAL_URL") or f"http://127.0.0.1:{port}/mcp"
    api_key = os.environ.get("MCP_API_KEY", api_key)
    poll = args.poll if args.poll is not None else _env_float("CRM_POLL_S", poll)
    heartbeat = (
        args.heartbeat if args.heartbeat is not None else _env_float("CRM_HEARTBEAT_S", heartbeat)
    )
    enabled = (
        args.enabled
        if args.enabled is not None
        else _env_bool("LAUNCHER_ENABLED", launcher_enabled)
    )
    # Resolve the approval-hook entry point ONCE, here at startup, from the launcher's
    # OWN PATH (never the repo). None when enabled => _serve refuses to arm (fail-closed):
    # we never silently spawn UNGATED workers when the operator asked for the gate.
    approval_hook_cmd: str | None = None
    if approval_hook_enabled:
        approval_hook_cmd = shutil.which("fast-mcp-claude-hook")
        if approval_hook_cmd is None:
            _log(
                "APPROVAL HOOK ENABLED but 'fast-mcp-claude-hook' is not on PATH; the "
                "launcher will REFUSE TO ARM (fail-closed) rather than spawn ungated "
                "workers. Install the package / fix PATH, then restart."
            )
    approval_socket_path = (
        os.environ.get("CRM_APPROVAL_SOCKET")
        or approval_socket_path
        or os.path.expanduser("~/.fast-mcp-claude/launcher-approval.sock")
    )
    return LauncherConfig(
        identity=identity,
        local_url=local_url,
        api_key=api_key,
        poll=poll,
        heartbeat=heartbeat,
        enabled=enabled,
        claude_bin=claude_bin,
        cwd_allowlist=cwd_allowlist,
        tools_ceiling=tools_ceiling,
        max_concurrent=max_concurrent,
        task_timeout_s=task_timeout_s,
        reply_max_bytes=reply_max_bytes,
        setting_sources=setting_sources,
        mcp_auth_enabled=mcp_auth_enabled,
        mcp_api_key_present=bool(api_key),
        approval_hook_enabled=approval_hook_enabled,
        approval_hook_cmd=approval_hook_cmd,
        approval_decision_timeout_s=approval_decision_timeout_s,
        approval_auto_pass_tools=approval_auto_pass_tools,
        approval_hook_selftest=approval_hook_selftest,
        approval_socket_path=approval_socket_path,
        announce_token=f"{os.getpid()}:{os.urandom(6).hex()}",
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


# ----------------------------------------------------------------- identity guard


def validate_launcher_identity(identity: str) -> str:
    """Hard-fail unless `identity` passes the server's SESSION_RE.

    The identity is the inbox mailbox key: send_prompt(recipient_session=<identity>)
    only routes here if the server accepts the same string. f"{peer_name}_launcher"
    must therefore satisfy ^[a-zA-Z0-9_.-]{1,128}$ — note '/' and ':' are REJECTED,
    underscore is fine. A peer_name with a slash/colon would produce a dead mailbox,
    so we refuse to start rather than silently never receive work.
    """
    if not isinstance(identity, str) or not SESSION_RE.match(identity):
        raise ValueError(
            f"launcher identity {identity!r} is invalid: must match {SESSION_RE.pattern} "
            "(peer_name must not contain '/' or ':'; underscore is allowed). "
            "Fix peer_name in .env or pass --identity."
        )
    return identity


# --------------------------------------------------------------- task envelope


class EnvelopeError(Exception):
    """A task envelope was malformed or violated policy. Carries the reply payload."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload.get("error", "bad_envelope"))
        self.payload = payload


@dataclass
class TaskEnvelope:
    task: str
    cwd: str  # the RESOLVED, allowlist-checked directory (realpath)
    allowed_tools: list[str]
    model: str | None
    timeout_s: float


def parse_envelope(prompt: str, cfg: LauncherConfig) -> TaskEnvelope:
    """Parse + validate a claimed message's `prompt` JSON task envelope.

    Raises EnvelopeError (with a ready-to-send reply payload) on any problem; NEVER
    falls back to running a raw string in a default cwd (that would be silent
    wrong-repo execution).
    """
    try:
        obj = json.loads(prompt)
    except (json.JSONDecodeError, TypeError) as e:
        raise EnvelopeError(
            {"ok": False, "error": "bad_envelope", "detail": f"prompt is not JSON: {e}"}
        ) from e
    if not isinstance(obj, dict):
        raise EnvelopeError(
            {"ok": False, "error": "bad_envelope", "detail": "envelope must be a JSON object"}
        )

    task = obj.get("task")
    cwd = obj.get("cwd")
    if not isinstance(task, str) or not task.strip():
        raise EnvelopeError(
            {"ok": False, "error": "bad_envelope", "detail": "missing/invalid 'task' (string)"}
        )
    if not isinstance(cwd, str) or not cwd.strip():
        raise EnvelopeError(
            {"ok": False, "error": "bad_envelope", "detail": "missing/invalid 'cwd' (string)"}
        )

    resolved_cwd = _resolve_cwd(cwd, cfg.cwd_allowlist)

    allowed_tools = _resolve_allowed_tools(obj.get("allowed_tools"), cfg.tools_ceiling)

    model = obj.get("model")
    if model is not None and not isinstance(model, str):
        raise EnvelopeError(
            {"ok": False, "error": "bad_envelope", "detail": "'model' must be a string"}
        )

    timeout_s = _resolve_timeout(obj.get("timeout_s"), cfg.task_timeout_s)

    return TaskEnvelope(
        task=task,
        cwd=str(resolved_cwd),
        allowed_tools=allowed_tools,
        model=model,
        timeout_s=timeout_s,
    )


def _resolve_cwd(raw: str, allowlist: list[Path]) -> Path:
    """Realpath `raw` (following symlinks) and verify it sits under an allowed root.

    Mirrors validate_workspace_path's symlink-escape guard: resolve the FULL path
    then relative_to() each root, so a symlink whose target escapes the allowlist is
    rejected.
    """
    if not allowlist:
        raise EnvelopeError(
            {
                "ok": False,
                "error": "cwd_not_allowed",
                "detail": "launcher_cwd_allowlist is empty (no cwd permitted)",
                "allowed": [],
            }
        )
    if "\x00" in raw:
        raise EnvelopeError(
            {"ok": False, "error": "bad_envelope", "detail": "cwd contains null byte"}
        )
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise EnvelopeError(
            {"ok": False, "error": "cwd_not_allowed", "detail": f"cwd does not resolve: {e}",
             "allowed": [str(r) for r in allowlist]}
        ) from e
    if not resolved.is_dir():
        raise EnvelopeError(
            {"ok": False, "error": "cwd_not_allowed", "detail": "cwd is not a directory",
             "allowed": [str(r) for r in allowlist]}
        )
    for root in allowlist:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise EnvelopeError(
        {"ok": False, "error": "cwd_not_allowed", "allowed": [str(r) for r in allowlist]}
    )


def _resolve_allowed_tools(raw: Any, ceiling: list[str]) -> list[str]:
    """An omitted allowed_tools uses the full ceiling; a provided one must be a subset."""
    if raw is None:
        return list(ceiling)
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise EnvelopeError(
            {"ok": False, "error": "bad_envelope", "detail": "'allowed_tools' must be a list[str]"}
        )
    requested = [t.strip() for t in raw if t.strip()]
    ceiling_set = set(ceiling)
    excess = [t for t in requested if t not in ceiling_set]
    if excess:
        raise EnvelopeError(
            {
                "ok": False,
                "error": "tools_exceed_ceiling",
                "ceiling": list(ceiling),
                "excess": excess,
            }
        )
    return requested


def _resolve_timeout(raw: Any, cap: float) -> float:
    """Clamp the envelope's timeout_s to the launcher's hard cap (also used when omitted)."""
    if raw is None:
        return float(cap)
    try:
        v = float(raw)
    except (TypeError, ValueError) as e:
        raise EnvelopeError(
            {"ok": False, "error": "bad_envelope", "detail": "'timeout_s' must be a number"}
        ) from e
    if v <= 0:
        return float(cap)
    return min(v, float(cap))


# ----------------------------------------------------------------- reply shaping


def _truncate_middle(s: str, budget: int) -> tuple[str, bool]:
    """Keep head+tail of `s` so its UTF-8 length is <= budget. Returns (text, truncated)."""
    if budget <= 0:
        return ("", bool(s))
    b = s.encode("utf-8")
    if len(b) <= budget:
        return (s, False)
    marker = "\n...[truncated]...\n"
    mlen = len(marker.encode("utf-8"))
    if budget <= mlen:
        # No room for head+tail; just keep a hard prefix.
        return (b[:budget].decode("utf-8", "ignore"), True)
    half = (budget - mlen) // 2
    head = b[:half].decode("utf-8", "ignore")
    tail = b[len(b) - half:].decode("utf-8", "ignore")
    return (head + marker + tail, True)


def shape_reply(
    *,
    ok: bool,
    exit_code: int | None,
    timed_out: bool,
    duration_s: float,
    result: str,
    stderr_tail: str,
    claude_session_id: str | None,
    cost_usd: float | None,
    is_error: bool | None,
    num_turns: int | None,
    reply_max_bytes: int,
) -> str:
    """Build the JSON-string reply, pre-truncating result + stderr_tail so the ENCODED
    object is <= reply_max_bytes. Truncation is correctness-critical: a reply above the
    server's 4 MB cap is rejected and the controller hangs until TTL.
    """
    truncated = False

    def encode(res: str, err: str) -> str:
        return json.dumps(
            {
                "ok": ok,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_s": round(duration_s, 3),
                "result": res,
                "stderr_tail": err,
                "claude_session_id": claude_session_id,
                "cost_usd": cost_usd,
                "is_error": is_error,
                "num_turns": num_turns,
                "truncated": truncated,
            }
        )

    # First pass with full values to measure the fixed-overhead (everything but
    # result/stderr_tail).
    encoded = encode(result, stderr_tail)
    if len(encoded.encode("utf-8")) <= reply_max_bytes:
        return encoded

    truncated = True
    # Overhead = encoded size with result/stderr emptied. Whatever is left is the
    # budget we split between result (favored) and stderr_tail.
    overhead = len(encode("", "").encode("utf-8"))
    remaining = reply_max_bytes - overhead
    if remaining < 0:
        remaining = 0
    # Give stderr a modest slice, the rest to result.
    err_budget = min(STDERR_TAIL_BYTES, remaining // 4)
    new_err, _ = _truncate_middle(stderr_tail, err_budget)
    result_budget = remaining - len(new_err.encode("utf-8"))
    new_result, _ = _truncate_middle(result, max(0, result_budget))
    encoded = encode(new_result, new_err)
    # Final safety: if still over (multi-byte boundary slack), shrink result harder.
    while len(encoded.encode("utf-8")) > reply_max_bytes and new_result:
        new_result, _ = _truncate_middle(new_result, max(0, len(new_result.encode("utf-8")) - 256))
        encoded = encode(new_result, new_err)
        if not new_result:
            break
    return encoded


def parse_claude_json(stdout: str) -> dict[str, Any]:
    """Parse `claude -p --output-format json` stdout (a single JSON object).

    Returns a dict with normalized keys; on parse failure returns a fallback marking
    is_error=True and carrying the raw stdout tail as the result.
    """
    text = stdout.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return {
                "result": obj.get("result", ""),
                "session_id": obj.get("session_id"),
                "total_cost_usd": obj.get("total_cost_usd"),
                "is_error": obj.get("is_error"),
                "num_turns": obj.get("num_turns"),
                "_parsed": True,
            }
    except (json.JSONDecodeError, TypeError):
        pass
    tail, _ = _truncate_middle(text, 8192)
    return {
        "result": tail,
        "session_id": None,
        "total_cost_usd": None,
        "is_error": True,
        "num_turns": None,
        "_parsed": False,
    }


# ----------------------------------------------------------------- subprocess


def _scrubbed_env() -> dict[str, str]:
    """os.environ minus the mesh bearer (MCP_API_KEY) and CRM_* vars; HOME/PATH kept."""
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        if k in _SCRUB_ENV_EXACT:
            continue
        if any(k.startswith(pfx) for pfx in _SCRUB_ENV_PREFIX):
            continue
        out[k] = v
    return out


def _base_tool_names(grants: list[str]) -> list[str]:
    """Derive the BASE tool names from full grant specs, order-stable and deduped.

    A grant spec is the tool name optionally followed by a "(...)" matcher, e.g.
    "Bash(uv run*)" -> "Bash", "Read" -> "Read". The base is the text before the
    first "(", stripped. Used for claude --tools (the base-set RESTRICTION: tools not
    listed do not exist for the session), distinct from --allowedTools (which only
    AUTO-APPROVES the full specs). Without --tools, permissionless built-ins
    (Read/Glob/Grep/...) would remain available regardless of the ceiling.
    """
    seen: set[str] = set()
    out: list[str] = []
    for g in grants:
        base = g.split("(", 1)[0].strip()
        if base and base not in seen:
            seen.add(base)
            out.append(base)
    return out


def _approval_hook_settings(cfg: LauncherConfig) -> str:
    """Build the --settings JSON that arms the launcher-controlled PreToolUse approval hook.

    SECURITY: the hook command carries NO mesh credential — only the launcher-owned approval
    SOCKET PATH (not a secret). The worker's hook talks to that socket; the LAUNCHER (which
    holds the bearer) relays request_approval/await_decision to the mesh. So even though the
    worker can read its own argv (same uid), it never obtains a credential it could use to
    self-approve or spoof the mesh. The hook path comes from shutil.which (launcher-resolved),
    the socket path is launcher-controlled — the repo at env.cwd has ZERO influence (this rides
    on --settings, NOT --setting-sources which stays "", so repo .claude/settings.json never
    loads). matcher "*" gates every tool; CRM_AUTO_PASS_TOOLS lets read-only ones skip the relay.
    Always json.dumps (never hand-format): claude SILENTLY IGNORES a --settings object that fails
    validation, which would disarm the gate.
    """
    hook_cmd = (
        f"CRM_HOOK_SOCKET={shlex.quote(cfg.approval_socket_path)} "
        f"CRM_DECISION_TIMEOUT={cfg.approval_decision_timeout_s:g} "
        f"CRM_AUTO_PASS_TOOLS={shlex.quote(cfg.approval_auto_pass_tools)} "
        f"{shlex.quote(cfg.approval_hook_cmd or '')}"
    )
    return json.dumps(
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": hook_cmd}]}
                ]
            }
        }
    )


def _build_cmd(env: TaskEnvelope, cfg: LauncherConfig) -> list[str]:
    cmd = [
        cfg.claude_bin,
        "-p",
        env.task,
        "--output-format",
        "json",
        # Always pass --setting-sources EXACTLY as configured, including the empty
        # string (load NO settings). Omitting the flag would load CLI defaults, and a
        # repo's .claude/settings.json hooks run arbitrary commands BYPASSING the tools
        # ceiling. Phase 3 will deliberately flip this to arm the approval hook.
        "--setting-sources",
        cfg.setting_sources,
        # A repo's .mcp.json must never hand the worker MCP servers / bearers.
        "--strict-mcp-config",
        # BASE-SET RESTRICTION: tools not listed do not exist for the session. Empty
        # string => the worker gets NO tools (pure reasoning). This is the actual
        # ceiling enforcement; --allowedTools below only auto-approves.
        "--tools",
        ",".join(_base_tool_names(env.allowed_tools)),
    ]
    if env.allowed_tools:
        cmd += ["--allowedTools", ",".join(env.allowed_tools)]
    if env.model:
        cmd += ["--model", env.model]
    if cfg.approval_hook_enabled and cfg.approval_hook_cmd:
        # Arm the launcher-controlled approval hook via the INDEPENDENT --settings flag
        # (additive to --setting-sources "", which stays empty so NO repo hooks load).
        cmd += ["--settings", _approval_hook_settings(cfg)]
    return cmd


@dataclass
class RunResult:
    exit_code: int | None
    timed_out: bool
    duration_s: float
    stdout: str
    stderr: str


@dataclass(eq=False)  # identity hash so instances can live in a set
class _LiveProc:
    """A spawned task's process, tracked for shutdown so we can SIGTERM its group."""

    proc: Any
    pgid: int | None


async def _read_capped(stream: Any, cap: int) -> bytes:
    """Read `stream` to EOF, retaining only the LAST `cap` bytes seen (a rolling tail
    window) instead of accumulating the full stream in memory. Memory held is O(cap) at
    any time regardless of total stream size -- the fix for AC#1's unbounded
    proc.communicate() buffering (the ONLY place output was previously bounded was AFTER
    communicate() returned, in the reply-shaping step, by which point the full buffers
    already existed).

    Downstream truncation is already tail-oriented (stderr_tail takes the last
    STDERR_TAIL_BYTES of stdout/stderr; _truncate_middle keeps head+tail of the parsed
    *result* field), so keeping the tail here preserves exactly the bytes that already
    matter for a legitimate task's reply. A task whose raw output exceeds `cap` was never
    going to round-trip through parse_claude_json as valid JSON anyway and already falls
    back to its is_error=True path.
    """
    if stream is None:
        return b""
    chunks: deque[bytes] = deque()
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        while chunks and total - len(chunks[0]) >= cap:
            total -= len(chunks.popleft())
    data = b"".join(chunks)
    return data[-cap:] if len(data) > cap else data


async def _drain_capped(
    stdout_task: "asyncio.Task[bytes]", stderr_task: "asyncio.Task[bytes]"
) -> tuple[bytes, bytes]:
    """Best-effort collect the bounded reader tasks once the process has exited (or been
    force-killed): their pipes reach EOF shortly after, but this must never hang a task
    handler or shutdown indefinitely."""
    try:
        return await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task), timeout=_READ_DRAIN_TIMEOUT_S
        )
    except (asyncio.TimeoutError, Exception):
        for t in (stdout_task, stderr_task):
            if not t.done():
                t.cancel()
        return (b"", b"")


async def _run_claude(
    env: TaskEnvelope,
    cfg: LauncherConfig,
    live: set["_LiveProc"],
) -> RunResult:
    """Spawn `claude -p ...` in its own process group, enforce a wall-clock timeout.

    stdout/stderr are streamed and capped as they're produced (AC#1, see _read_capped)
    rather than accumulated fully via proc.communicate().

    On timeout: SIGTERM the process GROUP, wait KILL_GRACE_S, then SIGKILL the group
    (AC#2, see _kill_group).
    """
    cmd = _build_cmd(env, cfg)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=env.cwd,
        start_new_session=True,  # own process group, so killpg reaches children
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_scrubbed_env(),
    )
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None
    lp = _LiveProc(proc=proc, pgid=pgid)
    live.add(lp)
    _log(f"spawned pid={proc.pid} pgid={pgid} cwd={env.cwd} timeout={env.timeout_s}s")

    stdout_task = asyncio.ensure_future(_read_capped(proc.stdout, MAX_SUBPROCESS_OUTPUT_BYTES))
    stderr_task = asyncio.ensure_future(_read_capped(proc.stderr, MAX_SUBPROCESS_OUTPUT_BYTES))

    timed_out = False
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=env.timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            _log(f"pid={proc.pid} timed out after {env.timeout_s}s; killing process group")
            await _kill_group(lp)
            # _kill_group guarantees no process remains in the group; reap the leader
            # (bounded defensively) so proc.returncode is populated.
            try:
                await asyncio.wait_for(proc.wait(), timeout=_READ_DRAIN_TIMEOUT_S)
            except asyncio.TimeoutError:
                pass
        stdout_b, stderr_b = await _drain_capped(stdout_task, stderr_task)
    finally:
        live.discard(lp)

    duration = time.monotonic() - started
    return RunResult(
        exit_code=proc.returncode,
        timed_out=timed_out,
        duration_s=duration,
        stdout=stdout_b.decode("utf-8", "replace"),
        stderr=stderr_b.decode("utf-8", "replace"),
    )


async def _drain(proc: Any) -> tuple[bytes, bytes]:
    """Best-effort collect any remaining output after a kill (used only by the approval-hook
    self-test's throwaway subprocess, whose output is small and launcher-controlled --
    AC#1's unbounded-buffering concern is specific to arbitrary spawned TASKS, see
    _read_capped)."""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except (asyncio.TimeoutError, Exception):
        return (b"", b"")


def _group_alive(pgid: int) -> bool:
    """True if any process still belongs to process group `pgid`. Signal 0 is a pure
    existence probe -- no signal is actually delivered; os.killpg raises
    ProcessLookupError (ESRCH) once no process in that group remains.

    Also treat PermissionError (EPERM) as "not alive": our own spawned descendants are
    always same-uid, so we always have permission to signal them; EPERM here means the OS
    has already recycled this pgid for an unrelated process we never spawned (a narrow but
    real pgid-reuse race under the polling window this function enables) -- there is
    nothing of OURS left to find, and we must never risk signaling a process group we
    don't actually own.
    """
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


async def _kill_group(lp: _LiveProc) -> None:
    """SIGTERM the process group, then verify the WHOLE group -- not just the leader --
    has exited within the grace period before considering the kill complete (AC#2). There
    is no asyncio primitive for "wait for a process group" (only for our single direct
    child), so we poll group-wide existence via _group_alive. A grandchild that ignores
    SIGTERM, or the leader itself lingering, is force-killed with a group-wide SIGKILL if
    anything is still alive once the grace period elapses.
    """
    if lp.pgid is None:
        try:
            lp.proc.terminate()
        except ProcessLookupError:
            return
        await asyncio.sleep(KILL_GRACE_S)
        try:
            lp.proc.kill()
        except ProcessLookupError:
            pass
        return
    try:
        os.killpg(lp.pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + KILL_GRACE_S
    while True:
        if not _group_alive(lp.pgid):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(_KILL_POLL_INTERVAL_S, remaining))
    if not _group_alive(lp.pgid):
        return
    try:
        os.killpg(lp.pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ----------------------------------------------------------------- reply sender


class _ClientBox:
    """Mutable holder for the bridge's CURRENT connection.

    A task handler is created against whichever connection is live when it starts, but its
    reply may not run until well after `_bridge` has reconnected and replaced that connection
    (a transport blip, a dead-session rebuild). Passing this box (instead of the raw client)
    lets `_send_reply` re-read `.client` on every retry attempt, so a reply started against an
    old connection picks up whatever connection is CURRENT instead of retrying forever against
    one that has already closed (AC#3)."""

    def __init__(self) -> None:
        self.client: Any = None


# Attempts * backoff must comfortably outlast a heartbeat-detected reconnect (up to
# ~heartbeat * _ANNOUNCE_FAILS_BEFORE_RECONNECT to detect a dead session, plus reconnect
# backoff to rebuild) so a task's reply doesn't give up while the bridge is mid-recovery
# (AC#3). Module-level so tests can shrink them like KILL_GRACE_S.
_REPLY_RETRY_ATTEMPTS = 8
_REPLY_RETRY_BACKOFF_S = 1.0


async def _send_reply(client_source: Any, message_id: str, response: str) -> None:
    """reply() to the local server; retry several times with backoff (the controller hangs
    until TTL otherwise).

    `client_source` is either a live client directly, or a `_ClientBox` whose `.client` is
    re-read on every attempt (see `_ClientBox`)."""
    last_err: Exception | None = None
    for attempt in range(_REPLY_RETRY_ATTEMPTS):
        client = client_source.client if isinstance(client_source, _ClientBox) else client_source
        if client is None:
            last_err = RuntimeError("no live client available for reply")
        else:
            try:
                res = await client.call_tool(
                    "reply", {"message_id": message_id, "response": response}
                )
                data = _result_data(res)
                if data.get("success"):
                    return
                last_err = RuntimeError(f"reply not accepted: {data}")
            except Exception as e:  # transport/encoding failure
                last_err = e
        await asyncio.sleep(_REPLY_RETRY_BACKOFF_S * (attempt + 1))
    _log(f"reply failed for message_id={message_id} after retries: {last_err}")


# ----------------------------------------------------------------- task handler


async def _handle_task(
    client: Any,
    msg: dict[str, Any],
    cfg: LauncherConfig,
    sem: asyncio.Semaphore,
    live: set["_LiveProc"],
    running: "_Counter",
    inflight: set[str],
) -> None:
    """Run one claimed task end-to-end. Structurally guarantees EXACTLY ONE reply on
    EVERY exit path. The slot (sem) is already acquired by the caller and is released
    here when the task finishes.

    `inflight` tracks this process's currently-running message_ids so the reconnect
    reaper can SKIP them (a mid-task transport blip must never fail a running task by
    reaping its own 'delivered' row as orphaned)."""
    message_id = str(msg.get("id", ""))
    running.value += 1
    inflight.add(message_id)
    try:
        try:
            env = parse_envelope(str(msg.get("prompt", "")), cfg)
        except EnvelopeError as ee:
            _log(f"envelope rejected for {message_id}: {ee.payload}")
            await _send_reply(client, message_id, json.dumps(ee.payload))
            return

        try:
            run = await _run_claude(env, cfg, live)
        except FileNotFoundError as e:
            await _send_reply(
                client,
                message_id,
                json.dumps({"ok": False, "error": "spawn_failed", "detail": str(e)[:500]}),
            )
            return
        except Exception as e:
            await _send_reply(
                client,
                message_id,
                json.dumps({"ok": False, "error": "spawn_failed", "detail": str(e)[:500]}),
            )
            return

        parsed = parse_claude_json(run.stdout)
        is_error = parsed.get("is_error")
        ok = (run.exit_code == 0) and (not run.timed_out) and (is_error is not True)
        stderr_tail = run.stderr[-STDERR_TAIL_BYTES:] if run.stderr else ""
        reply = shape_reply(
            ok=ok,
            exit_code=run.exit_code,
            timed_out=run.timed_out,
            duration_s=run.duration_s,
            result=str(parsed.get("result", "")),
            stderr_tail=stderr_tail,
            claude_session_id=parsed.get("session_id"),
            cost_usd=parsed.get("total_cost_usd"),
            is_error=is_error,
            num_turns=parsed.get("num_turns"),
            reply_max_bytes=cfg.reply_max_bytes,
        )
        await _send_reply(client, message_id, reply)
    except asyncio.CancelledError:
        # Shutdown path handles in-flight replies; just stop.
        raise
    except Exception as e:
        _log(f"internal error handling {message_id}: {e}\n{traceback.format_exc()}")
        await _send_reply(
            client,
            message_id,
            json.dumps({"ok": False, "error": "launcher_internal", "detail": str(e)[:500]}),
        )
    finally:
        running.value -= 1
        inflight.discard(message_id)
        sem.release()


# ----------------------------------------------------------------- stale reaper


async def _reap_stale_claims(
    client: Any, cfg: LauncherConfig, inflight: set[str] | None = None
) -> None:
    """Fail-fast every 'delivered' row addressed to OUR identity — orphaned by a
    previous crash mid-task — so the controller doesn't hang to the 7-day TTL. Runs
    BEFORE the poll loop claims any new work. Other identities' rows are untouched.

    `inflight` is THIS process's currently-running message_ids; any matching row is a
    task we are actively handling (e.g. on a reconnect after a transport blip), NOT an
    orphan — we SKIP it. Reaping it would fail a running task: its row goes back to
    'replied', the worker's real reply then hits NOT_REPLIABLE and the result is lost.

    Mailbox-coverage caveat (server-side, out of scope): list_messages caps limit at
    200, newest-first, with NO recipient filter — under heavy mailbox traffic this
    launcher's orphaned rows can fall outside the 200-row page and stay invisible. The
    real fix is a server-side recipient filter; here we only log loudly so the operator
    can diagnose.
    """
    inflight = inflight or set()
    try:
        res = await client.call_tool("list_messages", {"status": "delivered", "limit": 200})
    except Exception as e:
        _log(f"reaper: list_messages failed (continuing): {e}")
        return
    data = _result_data(res)
    rows = data.get("messages") or []
    scanned = len(rows)
    reaped = 0
    skipped_live = 0
    for row in rows:
        if str(row.get("recipient_session") or "") != cfg.identity:
            continue
        mid = str(row.get("id", ""))
        if mid in inflight:
            skipped_live += 1
            continue  # a task we are actively running — not an orphan
        await _send_reply(
            client,
            mid,
            json.dumps({"ok": False, "error": "launcher_restarted_task_lost"}),
        )
        reaped += 1
    # Loud line so the operator can diagnose the 200-row blind spot under heavy traffic.
    _log(
        f"reaper: scanned {scanned} delivered row(s) (server caps this page at 200, "
        f"newest-first, no recipient filter — older orphaned rows beyond the page are "
        f"NOT visible); reaped {reaped}, skipped {skipped_live} live in-flight, "
        f"for identity={cfg.identity}"
    )


# ----------------------------------------------------------------- poll loop


def _announce_metadata(cfg: LauncherConfig) -> dict[str, Any]:
    return {
        "role": "launcher",
        "version": __version__,
        "cwd_allowlist": [str(p) for p in cfg.cwd_allowlist],
        "tools_ceiling": list(cfg.tools_ceiling),
        "max_concurrent": cfg.max_concurrent,
        # ECA-71 owner-token: identifies THIS launcher process to the server's identity guard.
        "announce_token": cfg.announce_token or None,
    }


async def _heartbeat_loop(
    client: Any,
    cfg: LauncherConfig,
    running: "_Counter",
    reconnect_needed: asyncio.Event,
    owner_confirmed: asyncio.Event,
    owner_wait_abandoned: asyncio.Event,
) -> None:
    """Independent task: announce presence every cfg.heartbeat seconds (like channel.py).

    After _ANNOUNCE_FAILS_BEFORE_RECONNECT consecutive NON-auth failures, set reconnect_needed
    and exit so the bridge rebuilds the client: a `fast-mcp-claude` restart shows up here as a
    persistent 'Session terminated', and without this the launcher would announce-fail forever
    and drop out of who() while the main loop stayed blocked on a dead session (GH #3). Auth
    failures never trip a rebuild (reconnecting won't fix a bad bearer, and would risk the 60s
    endpoint lockout).

    ECA-71 owner-token gate (AC#1/#2): `owner_confirmed` is set on every successful announce on
    THIS connection and cleared on any non-success (most notably IDENTITY_LIVE_ELSEWHERE — a
    second live launcher already owns this identity). `_bridge` gates reaping and claiming on
    this event so a duplicate/illegitimate instance never reaps the real owner's in-flight tasks
    nor competes for new mailbox work while refused.

    `owner_wait_abandoned` is a SEPARATE escape valve for a bad bearer specifically: an
    auth-shaped exception never produces a well-formed response (so it can never BE an
    IDENTITY_LIVE_ELSEWHERE refusal), and never trips reconnect_needed either (see above) — so
    without this, a persistently bad bearer would block `_bridge`'s owner-confirmation gate
    forever with zero evidence of an actual identity conflict. After
    _AUTH_FAILS_BEFORE_GIVING_UP consecutive auth failures it is set so the gate proceeds anyway
    (every subsequent mesh call will fail identically on the same bad bearer, so this doesn't
    let a duplicate instance actually reap/claim anything — it only lets the EXISTING poll-loop
    error handling take back over, matching pre-ECA-71 behavior). Cleared the moment ANY
    well-formed response (success or refusal) is observed again, so a genuine refusal discovered
    once connectivity recovers still re-engages the gate."""
    consecutive = 0
    consecutive_auth = 0
    refusal_logged = False
    while True:
        try:
            res = await client.call_tool(
                "announce",
                {
                    "identity": cfg.identity,
                    "summary": f"launcher: {running.value}/{cfg.max_concurrent} tasks",
                    "metadata": _announce_metadata(cfg),
                },
            )
            consecutive = 0
            consecutive_auth = 0
            owner_wait_abandoned.clear()
            data = _result_data(res)
            if data.get("success"):
                owner_confirmed.set()
                refusal_logged = False
            else:
                # Fail-closed: only an explicit success counts as confirmed ownership. Most
                # commonly IDENTITY_LIVE_ELSEWHERE (a second live launcher already holds this
                # identity) — log loudly once so the operator can kill the offender. pm2 runs
                # one launcher per peer, so this is a defensive backstop; the real fix lives in
                # the channel sidecar.
                owner_confirmed.clear()
                err = data.get("error") if isinstance(data.get("error"), dict) else {}
                if err.get("code") == "IDENTITY_LIVE_ELSEWHERE" and not refusal_logged:
                    _log(
                        f"announce REFUSED for identity={cfg.identity!r}: another live launcher "
                        f"already holds it (IDENTITY_LIVE_ELSEWHERE). This process will NOT reap "
                        f"or claim work under this identity until ownership is confirmed. Kill "
                        f"the duplicate if this process should own it. Detail: {data}"
                    )
                    refusal_logged = True
        except Exception as e:
            _log(f"announce failed (continuing): {e}")
            if any(h in str(e).lower() for h in _AUTH_HINTS):
                consecutive = 0  # auth won't recover via reconnect; never trip a rebuild
                consecutive_auth += 1
                if consecutive_auth >= _AUTH_FAILS_BEFORE_GIVING_UP:
                    if not owner_wait_abandoned.is_set():
                        _log(
                            f"announce auth-failed {consecutive_auth}x consecutively; giving up "
                            "waiting for owner confirmation (fix the bearer to recover — this "
                            "does NOT let a duplicate instance claim work, since every mesh call "
                            "will fail identically on the same bad bearer)"
                        )
                    owner_wait_abandoned.set()
            else:
                consecutive_auth = 0
                consecutive += 1
                if consecutive >= _ANNOUNCE_FAILS_BEFORE_RECONNECT:
                    _log(
                        f"announce failed {consecutive}x consecutively; signaling bridge to "
                        "rebuild the client (local server likely restarted)"
                    )
                    reconnect_needed.set()
                    return
        await asyncio.sleep(cfg.heartbeat)


async def _wait_for_instruction_or_reconnect(
    c: Any, cfg: LauncherConfig, reconnect_needed: asyncio.Event
) -> Any:
    """Long-poll wait_for_instruction, but abort with _ReconnectNeeded if the heartbeat signals a
    dead session while we're blocked — otherwise a server restart could leave us parked in a poll
    against a session the server has forgotten, forever (GH #3). Returns the raw tool result; any
    error from the call itself propagates (the bridge's outer handler reconnects on it too)."""
    call = asyncio.ensure_future(
        c.call_tool(
            "wait_for_instruction",
            {"recipient_session": cfg.identity, "timeout": cfg.poll},
        )
    )
    dead = asyncio.ensure_future(reconnect_needed.wait())
    try:
        await asyncio.wait({call, dead}, return_when=asyncio.FIRST_COMPLETED)
        if call.done():
            return call.result()  # re-raises a call error -> outer reconnect
        raise _ReconnectNeeded()  # heartbeat tripped while the poll was still blocked
    finally:
        # Settle whichever future we're abandoning so it can't leak as a pending task. The
        # in-flight call is cancelled here; the client context is about to be torn down anyway.
        for fut in (call, dead):
            if not fut.done():
                fut.cancel()
                try:
                    await fut
                except (asyncio.CancelledError, Exception):
                    pass


async def _wait_for_owner_confirmed_or_reconnect(
    owner_confirmed: asyncio.Event,
    reconnect_needed: asyncio.Event,
    owner_wait_abandoned: asyncio.Event,
) -> None:
    """Block until this connection's ownership is confirmed (a successful announce), the
    heartbeat gives up waiting on a persistently bad bearer (`owner_wait_abandoned` — see
    `_heartbeat_loop`), or abort with _ReconnectNeeded if the heartbeat gives up on the
    connection entirely first.

    AC#1/#2: `_bridge` must never reap or claim while ownership is unconfirmed, but blocking on
    `owner_confirmed.wait()` alone would deadlock forever if the heartbeat loop exits (signalling
    reconnect_needed) — or just never sets it (a persistently bad bearer never yields a
    well-formed response either way) — before ever announcing successfully on this connection.
    Racing all three events (mirrors `_wait_for_instruction_or_reconnect`) closes that gap."""
    if owner_confirmed.is_set() or owner_wait_abandoned.is_set():
        return
    confirmed = asyncio.ensure_future(owner_confirmed.wait())
    abandoned = asyncio.ensure_future(owner_wait_abandoned.wait())
    dead = asyncio.ensure_future(reconnect_needed.wait())
    try:
        await asyncio.wait({confirmed, abandoned, dead}, return_when=asyncio.FIRST_COMPLETED)
        if not owner_confirmed.is_set() and not owner_wait_abandoned.is_set():
            raise _ReconnectNeeded()
    finally:
        for fut in (confirmed, abandoned, dead):
            if not fut.done():
                fut.cancel()
                try:
                    await fut
                except (asyncio.CancelledError, Exception):
                    pass


async def _wait_for_relay_healthy_or_reconnect(
    relay_healthy: "asyncio.Event",
    reconnect_needed: "asyncio.Event",
) -> None:
    """Block until the approval relay is confirmed healthy (AC#3), or abort with
    _ReconnectNeeded if the heartbeat gives up on THIS mesh connection first -- a relay
    outage must never wedge `_bridge` behind a dead mesh session it should instead be
    reconnecting from (mirrors `_wait_for_owner_confirmed_or_reconnect`'s race). Unlike
    that gate, there is deliberately no "give up waiting" branch here -- see
    `_run_approval_relay_supervised`'s docstring for why blocking forever on a
    persistently-unhealthy relay is the CORRECT fail-closed behavior for AC#3, not a bug.
    """
    if relay_healthy.is_set():
        return
    healthy = asyncio.ensure_future(relay_healthy.wait())
    dead = asyncio.ensure_future(reconnect_needed.wait())
    try:
        await asyncio.wait({healthy, dead}, return_when=asyncio.FIRST_COMPLETED)
        if not relay_healthy.is_set():
            raise _ReconnectNeeded()
    finally:
        for fut in (healthy, dead):
            if not fut.done():
                fut.cancel()
                try:
                    await fut
                except (asyncio.CancelledError, Exception):
                    pass


async def _bridge(cfg: LauncherConfig, relay_healthy: "asyncio.Event | None" = None) -> None:
    """Connect to the local server and pump inbox -> spawn until cancelled.

    Reconnect-with-backoff on transport errors (mirrors channel.py). On each
    reconnect we re-run the stale reaper before claiming, since a transport blip
    could have left a delivered row half-handled.

    `relay_healthy` (AC#3, None when the approval hook is disabled) gates gated-task
    claiming on the approval relay being confirmed up — see `_run_approval_relay_supervised`
    and `_wait_for_relay_healthy_or_reconnect`.
    """
    from fastmcp import Client

    # fastmcp 3.x Client takes the bearer as `auth=<token>` (a bare string is sent as
    # `Authorization: Bearer <token>`), NOT a `headers=` kwarg.
    client_kwargs: dict[str, Any] = {}
    if cfg.api_key:
        client_kwargs["auth"] = cfg.api_key

    sem = asyncio.Semaphore(cfg.max_concurrent)
    live: set[_LiveProc] = set()
    inflight: set[str] = set()  # message_ids this process is actively running
    tasks: set[asyncio.Task] = set()
    running = _Counter()
    backoff = 1.0
    reaped_once = False  # full reaper runs ONCE per process lifetime (first connect)
    # Survives reconnects (unlike `c` itself): task handlers reply through whichever
    # connection is CURRENT rather than the one they were created against (AC#3).
    box = _ClientBox()
    while True:
        heartbeat_task: asyncio.Task | None = None
        try:
            async with Client(cfg.local_url, **client_kwargs) as c:
                box.client = c
                backoff = 1.0
                _log(f"connected to {cfg.local_url} as identity={cfg.identity!r}")
                # Fresh per connection: the heartbeat sets reconnect_needed if it detects the
                # session has died (server restart), and the main poll races against it so a
                # blocked long-poll can't strand us on a dead session (GH #3). owner_confirmed
                # (ECA-71) is set by the heartbeat on THIS connection's first successful
                # announce and cleared on any refusal (IDENTITY_LIVE_ELSEWHERE) — AC#1/#2 gate
                # reaping and claiming on it below.
                reconnect_needed = asyncio.Event()
                owner_confirmed = asyncio.Event()
                owner_wait_abandoned = asyncio.Event()
                heartbeat_task = asyncio.create_task(
                    _heartbeat_loop(
                        c, cfg, running, reconnect_needed, owner_confirmed, owner_wait_abandoned
                    )
                )
                # AC#2: never reap or claim until THIS process's own ownership of the identity
                # has been confirmed by a successful announce on the current connection — a
                # duplicate/illegitimate instance must not reap the real owner's in-flight tasks
                # nor start claiming new mailbox work while its announce is refused.
                await _wait_for_owner_confirmed_or_reconnect(
                    owner_confirmed, reconnect_needed, owner_wait_abandoned
                )
                # Run the full reaper ONCE per process lifetime, at the first
                # successful connect (no tasks are in-flight yet, so every orphaned
                # 'delivered' row really is from a PREVIOUS crash). We still pass
                # `inflight` for defense-in-depth. On RECONNECTS (a transport blip) we
                # SKIP the reaper entirely: this process's own currently-running tasks
                # hold 'delivered' rows, and reaping them would fail the running task
                # (its row -> 'replied', the worker's real reply then hits
                # NOT_REPLIABLE and the result is lost). Prior-crash orphans were
                # already reaped at first connect.
                if not reaped_once:
                    await _reap_stale_claims(c, cfg, inflight)
                    reaped_once = True
                else:
                    _log("reconnect: skipping reaper (live in-flight tasks must not be reaped)")
                while True:
                    # Slot FIRST: never claim a task we can't run (claiming flips it
                    # to 'delivered'; a crash before reply would lose it).
                    await sem.acquire()
                    if reconnect_needed.is_set():
                        sem.release()  # heartbeat already flagged a dead session
                        raise _ReconnectNeeded()
                    if not owner_confirmed.is_set() and not owner_wait_abandoned.is_set():
                        # AC#1: a refusal discovered mid-run (our own heartbeat went stale and
                        # a legitimate takeover's announce won) must stop new claims too, not
                        # just gate the very first reap/claim right after connecting.
                        sem.release()
                        await _wait_for_owner_confirmed_or_reconnect(
                            owner_confirmed, reconnect_needed, owner_wait_abandoned
                        )
                        continue
                    if relay_healthy is not None and not relay_healthy.is_set():
                        # AC#3: never claim/spawn a gated task while the approval relay isn't
                        # confirmed listening — an unreachable relay makes the worker hook fall
                        # back to "ask", which does NOT override-deny a tool already covered by
                        # --allowedTools, so a call in this window would run completely ungated.
                        sem.release()
                        await _wait_for_relay_healthy_or_reconnect(relay_healthy, reconnect_needed)
                        continue
                    try:
                        res = await _wait_for_instruction_or_reconnect(c, cfg, reconnect_needed)
                    except Exception:
                        sem.release()  # give the slot back before reconnecting
                        raise
                    data = _result_data(res)
                    if not data.get("success"):
                        _log(f"wait_for_instruction returned error: {data}")
                        sem.release()
                        await asyncio.sleep(1.0)
                        continue
                    msg = data.get("message")
                    if not msg:
                        sem.release()  # timeout, no work
                        continue
                    if not owner_confirmed.is_set() and not owner_wait_abandoned.is_set():
                        # ECA-71 residual (mirrors channel.py's _inbox_loop): wait_for_instruction
                        # already claims the message server-side before returning, so a refusal
                        # discovered WHILE this poll was in flight can still surface a real
                        # message here even though we are no longer a confirmed owner. We cannot
                        # un-claim it, but we can refuse to act on it rather than silently running
                        # a task we may not legitimately own.
                        mid = str(msg.get("id", ""))
                        _log(f"claimed {mid} but ownership was refused mid-poll; bouncing")
                        await _send_reply(
                            box,
                            mid,
                            json.dumps({"ok": False, "error": "launcher_identity_uncertain"}),
                        )
                        sem.release()
                        continue
                    if relay_healthy is not None and not relay_healthy.is_set():
                        # AC#3 residual (mirrors the owner-token check above):
                        # wait_for_instruction already claims the message server-side before
                        # returning, so a relay crash discovered WHILE this poll was in flight
                        # can still surface a real message here even though the relay is no
                        # longer healthy. We cannot un-claim it, but we can bounce it rather
                        # than spawn it ungated.
                        mid = str(msg.get("id", ""))
                        _log(f"claimed {mid} but the approval relay is unhealthy; bouncing")
                        await _send_reply(
                            box,
                            mid,
                            json.dumps({"ok": False, "error": "launcher_relay_unavailable"}),
                        )
                        sem.release()
                        continue
                    _log(f"claimed task {msg.get('id')} from {msg.get('sender')}")
                    t = asyncio.create_task(
                        _handle_task(box, msg, cfg, sem, live, running, inflight)
                    )
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        except asyncio.CancelledError:
            await _shutdown(cfg, client_kwargs, live, tasks, heartbeat_task)
            raise
        except _ReconnectNeeded:
            # Expected control-flow signal (heartbeat saw a dead session), not a crash: rebuild
            # the client promptly and without a noisy traceback. The next connect re-announces.
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            _log("rebuilding client after detecting a dead session (server restart)")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        except Exception as e:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            _log(f"bridge error: {e}; reconnecting in {backoff:.0f}s\n{traceback.format_exc()}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


# Bound on _shutdown's fresh-connection reply sweep. A `Client(...)` connect has no timeout of
# its own (fastmcp's client_init_timeout defaults to disabled), so if the local server is ALSO
# going down but still accepting TCP connections without answering, an unbounded sweep could hang
# shutdown indefinitely — worse than the pre-fix behavior it replaces, which at least failed
# near-instantly (calling a closed client raises immediately). This keeps shutdown a bounded,
# best-effort operation: reply what we can within the window, then exit regardless.
_SHUTDOWN_REPLY_SWEEP_TIMEOUT_S = 20.0


async def _shutdown(
    cfg: LauncherConfig,
    client_kwargs: dict[str, Any],
    live: set["_LiveProc"],
    tasks: set[asyncio.Task],
    heartbeat_task: asyncio.Task | None,
) -> None:
    """On SIGTERM/SIGINT: SIGTERM live child groups, bounded grace, then reply
    launcher_shutdown for every in-flight task (always-reply discipline), then exit.

    AC#4: opens its OWN fresh connection for the reply sweep instead of reusing `_bridge`'s
    connection. By the time cancellation reaches `_bridge`'s `except asyncio.CancelledError`
    (which calls this), it has already unwound through that connection's own `async with`
    __aexit__ and torn it down — reusing it here would silently fail every list_messages/reply
    call below, exactly the bug this fixes. The whole sweep is bounded by
    _SHUTDOWN_REPLY_SWEEP_TIMEOUT_S so a hung/unreachable server can't stall shutdown forever."""
    if heartbeat_task is not None:
        heartbeat_task.cancel()
    # Snapshot in-flight message ids BEFORE cancelling the handlers (their finally
    # blocks would otherwise race us). We reply for them here instead.
    inflight_ids: list[str] = []
    for lp in list(live):
        await _kill_group(lp)
    for t in list(tasks):
        t.cancel()
    # Best-effort: drain cancellations.
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    # Reply launcher_shutdown for anything still delivered to us, via a FRESH connection (the
    # bridge's own connection is already closed by this point — see the docstring above).
    from fastmcp import Client

    async def _reply_sweep() -> None:
        async with Client(cfg.local_url, **client_kwargs) as client:
            res = await client.call_tool("list_messages", {"status": "delivered", "limit": 200})
            data = _result_data(res)
            for row in data.get("messages") or []:
                if str(row.get("recipient_session") or "") != cfg.identity:
                    continue
                inflight_ids.append(str(row.get("id", "")))
            for mid in inflight_ids:
                await _send_reply(
                    client,
                    mid,
                    json.dumps({"ok": False, "error": "launcher_shutdown"}),
                )

    try:
        await asyncio.wait_for(_reply_sweep(), timeout=_SHUTDOWN_REPLY_SWEEP_TIMEOUT_S)
    except asyncio.TimeoutError:
        _log(
            f"shutdown reply sweep timed out after {_SHUTDOWN_REPLY_SWEEP_TIMEOUT_S:.0f}s "
            "(local server unreachable/hung) — some in-flight tasks may not have received "
            "launcher_shutdown; their controllers will hang until the message TTL expires"
        )
    except Exception as e:
        _log(f"shutdown reply sweep failed: {e}")
    _log(f"shutdown complete (replied launcher_shutdown for {len(inflight_ids)} task(s))")


# ----------------------------------------------------------------- preflight


@dataclass
class Preflight:
    ok: bool
    bin_path: str | None = None
    version: str | None = None
    reason: str = ""


async def _preflight(cfg: LauncherConfig) -> Preflight:
    """Resolve the claude binary and probe its version. A missing binary is treated as
    disabled-with-error: do NOT poll (a claim we can't run is lost work) — log and idle.
    """
    bin_path = shutil.which(cfg.claude_bin)
    if not bin_path:
        return Preflight(ok=False, reason=f"claude binary {cfg.claude_bin!r} not found on PATH")
    version = None
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=VERSION_PROBE_TIMEOUT_S)
        version = (out or b"").decode("utf-8", "replace").strip() or None
    except Exception as e:
        _log(f"claude --version probe failed (continuing): {e}")
    return Preflight(ok=True, bin_path=bin_path, version=version)


async def _selftest_approval_hook(cfg: LauncherConfig, bin_path: str) -> bool:
    """Prove a --settings PreToolUse hook FIRES under --setting-sources "" before arming.

    Spawns a THROWAWAY `claude -p` whose hook touches a marker file and DENIES the tool;
    if the marker appears, --settings hooks execute and the gate is real. claude silently
    ignores a --settings object that fails validation (green exit, NO hook), so this is the
    only way to catch a disarmed gate. The hook DENIES, so the forced `echo` never runs (no
    side effects). Two attempts (the model must actually call the tool); returns True iff the
    hook fired. The caller idles (fail-closed) on False.
    """
    deny = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "launcher self-test",
            }
        }
    )
    for attempt in (1, 2):
        try:
            with tempfile.TemporaryDirectory() as td:
                marker = Path(td) / "fired"
                hook = Path(td) / "selftest_hook.sh"
                hook.write_text(
                    "#!/bin/sh\n"
                    f"touch {shlex.quote(str(marker))}\n"
                    f"printf '%s' {shlex.quote(deny)}\n"
                )
                hook.chmod(0o700)
                settings = json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "*",
                                    "hooks": [{"type": "command", "command": str(hook)}],
                                }
                            ]
                        }
                    }
                )
                cmd = [
                    bin_path,
                    "-p",
                    "Use the Bash tool to run exactly this command: echo CRM_SELFTEST",
                    "--output-format",
                    "json",
                    "--setting-sources",
                    "",
                    "--strict-mcp-config",
                    "--tools",
                    "Bash",
                    "--allowedTools",
                    "Bash",
                    "--settings",
                    settings,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=td,
                    env=_scrubbed_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=90.0)
                except asyncio.TimeoutError:
                    _log(f"approval-hook self-test attempt {attempt} timed out; killing")
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await _drain(proc)
                if marker.exists():
                    return True
                _log(f"approval-hook self-test attempt {attempt}: hook did NOT fire")
        except Exception as e:
            _log(f"approval-hook self-test attempt {attempt} errored: {e}")
    return False


# ---------------------------------------------------------------- approval relay


async def _relay_decision(
    cfg: LauncherConfig, session_id: str, tool_name: str, tool_input: dict[str, Any]
) -> tuple[str, str]:
    """Run the request_approval -> await_decision loop on the mesh with the LAUNCHER's bearer.

    This is the worker hook's old job, moved here so the bearer never leaves the trusted
    launcher process. Returns (decision, reason); on any error falls through to "ask" so the
    worker defers to its --tools ceiling rather than silently allowing/denying.
    """
    from fastmcp import Client

    client_kwargs: dict[str, Any] = {}
    if cfg.api_key:
        client_kwargs["auth"] = cfg.api_key
    total = cfg.approval_decision_timeout_s
    async with Client(cfg.local_url, **client_kwargs) as c:
        req = await c.call_tool(
            "request_approval",
            {"session_id": session_id, "tool_name": tool_name, "tool_input": tool_input},
        )
        data = _result_data(req)
        if not data.get("success"):
            msg = (data.get("error") or {}).get("message", "request_approval failed")
            return "ask", f"crm: {msg}"
        approval_id = data["approval_id"]
        elapsed = 0.0
        while elapsed < total:
            chunk = min(25.0, total - elapsed)
            res = await c.call_tool(
                "await_decision", {"approval_id": approval_id, "timeout": chunk}
            )
            rdata = _result_data(res)
            if not rdata.get("success"):
                msg = (rdata.get("error") or {}).get("message", "await_decision failed")
                return "ask", f"crm: {msg}"
            if rdata.get("ready"):
                approval = rdata["approval"]
                decision = approval.get("decision") or "ask"
                reason = (approval.get("reason") or "").strip()
                if decision not in ("allow", "deny"):
                    decision = "ask"
                return decision, reason or f"controller decided: {decision}"
            elapsed += chunk
    return "ask", f"controller did not decide within {total:.0f}s"


async def _handle_relay_conn(
    reader: Any, writer: Any, cfg: LauncherConfig, sem: "asyncio.Semaphore"
) -> None:
    """One worker-hook connection: read the request, relay to the mesh, write the decision.

    The socket exposes ONLY this request/await capability (never approve_tool/reply), so a
    same-uid worker connecting directly can at most ask for approvals it can't grant. A
    semaphore (sized to max_concurrent, BEFORE we create any approval row) caps a malicious or
    buggy worker to the same ceiling that bounds every other worker-initiated mesh action, so
    it can't flood the local approval table or the operator's Teams DMs. Honest workers run
    PreToolUse synchronously (one in-flight hook each), so the cap is transparent to them."""
    async with sem:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=15.0)
            req = json.loads(line.decode("utf-8"))
            session_id = str(req.get("session_id") or "default")[:128]
            tool_name = str(req.get("tool_name") or "")
            tool_input = req.get("tool_input")
            if not isinstance(tool_input, dict):
                tool_input = {}
            decision, reason = await _relay_decision(cfg, session_id, tool_name, tool_input)
        except Exception as e:
            decision, reason = "ask", f"launcher relay error: {e}"
        try:
            writer.write((json.dumps({"decision": decision, "reason": reason}) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


async def _approval_relay_server(
    cfg: LauncherConfig, stop: "asyncio.Event", sem: "asyncio.Semaphore", ready: "asyncio.Event"
) -> None:
    """Listen on the launcher-owned unix socket and relay worker-hook approvals until stop.

    Sets `ready` once the socket is confirmed bound and accepting connections (AC#3), so a
    supervisor (see `_run_approval_relay_supervised`) can gate gated-task claiming on actual
    listening state rather than on this coroutine having merely been scheduled.
    """
    sock_path = cfg.approval_socket_path
    Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        os.unlink(sock_path)  # clear a stale socket from a prior run
    except FileNotFoundError:
        pass
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_relay_conn(r, w, cfg, sem), path=sock_path
    )
    try:
        os.chmod(sock_path, 0o600)
    except OSError:
        pass
    _log(f"approval relay listening on {sock_path}")
    ready.set()
    try:
        await stop.wait()
    finally:
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


async def _run_approval_relay_supervised(
    cfg: LauncherConfig,
    stop: "asyncio.Event",
    relay_sem: "asyncio.Semaphore",
    relay_healthy: "asyncio.Event",
) -> None:
    """Keep the approval relay server running until `stop`, restarting it with backoff if it
    exits or crashes early (AC#3).

    `relay_healthy` mirrors the server's actual up/down state -- SET only once THIS run's
    socket is confirmed bound+listening, CLEARED the instant it stops being so -- so
    `_bridge` can gate gated-task claiming on it instead of assuming a background task that
    was merely STARTED is actually listening and staying up.

    Unlike the ECA-71 owner-token gate (`_wait_for_owner_confirmed_or_reconnect`), there is
    deliberately no "give up waiting" escape valve for a persistently-failing relay: that
    gate's fallback exists because pre-ECA-71 behavior was to proceed with no gate at all
    (a rare-duplicate-instance backstop), whereas AC#3's entire purpose is the operator's
    explicit request to gate every tool call -- so a relay that can never come up must keep
    blocking gated-task claiming forever, not fail open.
    """
    backoff = _RELAY_RESTART_BACKOFF_S
    while not stop.is_set():
        ready = asyncio.Event()
        server = asyncio.create_task(_approval_relay_server(cfg, stop, relay_sem, ready))
        ready_wait = asyncio.ensure_future(ready.wait())
        try:
            await asyncio.wait({ready_wait, server}, return_when=asyncio.FIRST_COMPLETED)
            if ready.is_set():
                relay_healthy.set()
                backoff = _RELAY_RESTART_BACKOFF_S
            await server  # already done => returns/raises now; else blocks until stop/crash
        except asyncio.CancelledError:
            raise  # propagate OUR OWN cancellation (e.g. _serve shutting down) untouched
        except Exception as e:
            _log(f"approval relay error: {e}")
        finally:
            relay_healthy.clear()
            for fut in (ready_wait, server):
                if not fut.done():
                    fut.cancel()
                    try:
                        await fut
                    except (asyncio.CancelledError, Exception):
                        pass
        if stop.is_set():
            return
        _log(f"approval relay not running; retrying in {backoff:.0f}s")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _RELAY_RESTART_BACKOFF_MAX_S)


# ----------------------------------------------------------------- serve / main


async def _idle_forever(reason: str) -> None:
    """Log once, then sleep forever — safe under pm2 autorestart (no crash-loop)."""
    _log(reason)
    while True:
        await asyncio.sleep(3600)


async def _serve(cfg: LauncherConfig) -> None:
    # Identity guard: hard-fail with a clear message if peer_name made it invalid.
    validate_launcher_identity(cfg.identity)

    if not cfg.enabled:
        # STRICT OPT-IN: disabled => never poll, never claim. Idle forever.
        await _idle_forever(
            "launcher disabled (LAUNCHER_ENABLED=false); inert — no polling/claiming/spawning."
        )
        return

    # AUTH GUARD: if the LOCAL mesh endpoint is effectively unauthenticated (no
    # MCP_API_KEY OR auth disabled), a spawned worker on localhost could call
    # reply/send_prompt unauthenticated and spoof replies — env scrubbing is moot.
    # Treat exactly like the missing-binary path: idle, never poll.
    if not cfg.mcp_auth_enabled or not cfg.mcp_api_key_present:
        await _idle_forever(
            "LAUNCHER REFUSING TO ARM: local mesh endpoint is unauthenticated "
            f"(mcp_auth_enabled={cfg.mcp_auth_enabled}, "
            f"api_key_present={cfg.mcp_api_key_present}); "
            "a spawned worker could spoof replies. Staying alive but NOT polling. Set "
            "MCP_API_KEY and MCP_AUTH_ENABLED=true on the local server, then restart."
        )
        return

    pf = await _preflight(cfg)
    if not pf.ok:
        # Missing binary => disabled-with-error: idle, never poll (a claim we can't
        # run would be lost work).
        await _idle_forever(
            f"LAUNCHER DISABLED-WITH-ERROR: {pf.reason}. Staying alive but NOT polling. "
            "Install the claude CLI or fix launcher_claude_bin, then restart."
        )
        return

    # APPROVAL-GATE GUARD (Phase 3, fail-closed): if the operator armed the approval hook
    # but we couldn't resolve fast-mcp-claude-hook, refuse to arm rather than spawn UNGATED
    # workers under a falsely-believed gate.
    if cfg.approval_hook_enabled and not cfg.approval_hook_cmd:
        await _idle_forever(
            "LAUNCHER REFUSING TO ARM: approval hook enabled but 'fast-mcp-claude-hook' "
            "is not resolvable on PATH; refusing to spawn UNGATED workers. Fix PATH/install, "
            "then restart."
        )
        return
    # And prove the --settings PreToolUse hook actually FIRES before trusting it (claude
    # silently ignores a --settings object that fails validation — a green exit with NO
    # gate). Fail-closed: idle if it can't be proven.
    if cfg.approval_hook_enabled and cfg.approval_hook_selftest:
        if not await _selftest_approval_hook(cfg, pf.bin_path or cfg.claude_bin):
            await _idle_forever(
                "LAUNCHER REFUSING TO ARM: approval-hook self-test FAILED (a --settings "
                'PreToolUse hook did not fire under --setting-sources ""). The approval gate '
                "cannot be proven, so workers are NOT spawned. Check the claude CLI version / "
                "--settings handling, or set LAUNCHER_APPROVAL_HOOK_SELFTEST=false to bypass, "
                "then restart."
            )
            return
        _log("approval-hook self-test PASSED: --settings PreToolUse gate is armed")

    _log(
        f"starting launcher (identity={cfg.identity!r}, local={cfg.local_url}, "
        f"claude={pf.bin_path} version={pf.version!r}, max_concurrent={cfg.max_concurrent}, "
        f"cwd_allowlist={[str(p) for p in cfg.cwd_allowlist]})"
    )

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    # Approval relay: the launcher (holding the bearer) serves the worker-hook socket so the
    # worker never receives a mesh credential it could use to self-approve. relay_healthy
    # (AC#3) is created BEFORE the bridge so it always reflects the relay's real state; the
    # bridge gates gated-task claiming on it (never on task-creation order alone).
    relay_task: asyncio.Task | None = None
    relay_healthy: asyncio.Event | None = None
    if cfg.approval_hook_enabled:
        relay_healthy = asyncio.Event()
        relay_sem = asyncio.Semaphore(cfg.max_concurrent)
        relay_task = asyncio.create_task(
            _run_approval_relay_supervised(cfg, stop, relay_sem, relay_healthy)
        )
    bridge_task = asyncio.create_task(_bridge(cfg, relay_healthy))

    def _request_stop() -> None:
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            pass  # e.g. non-main thread / Windows — fall back to KeyboardInterrupt

    try:
        await stop.wait()
    finally:
        bridge_task.cancel()
        if relay_task is not None:
            relay_task.cancel()
        for t in (bridge_task, relay_task):
            if t is None:
                continue
            try:
                await t
            except asyncio.CancelledError:
                pass


def main() -> None:
    cfg = _resolve_config(sys.argv[1:])
    try:
        asyncio.run(_serve(cfg))
    except (KeyboardInterrupt, EOFError):
        pass
    except ValueError as e:
        # Identity guard hard-fail: print loudly and exit non-zero.
        print(f"[fast-mcp-claude-launcher] FATAL: {e}", file=sys.stderr, flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
