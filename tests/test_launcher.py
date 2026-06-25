"""Tests for the fast-mcp-claude-launcher sidecar.

Mirrors test_channel.py style: unit-test the pure/awaitable pieces with a FAKE local
client and a FAKE claude binary (a tiny python script printing canned claude-style
JSON). We never spawn the real CLI or touch the network.

Coverage:
  * config precedence (enabled CLI > env > Settings; identity default + override)
  * identity validation (mini2_launcher passes; '/'-containing peer_name hard-fails)
  * envelope parsing (valid, malformed JSON, missing task/cwd, cwd outside allowlist
    incl. symlink-escape, tools exceeding ceiling, timeout capping)
  * reply truncation under a tiny byte budget
  * always-reply: a handler that raises mid-spawn still posts exactly one
    launcher_internal reply
  * fake claude binary: success / nonzero-exit / sleep-forever timeout-kill paths
  * stale-claim reaper: only own-identity delivered rows get launcher_restarted_task_lost
  * disabled inertness: when disabled, no poll/claim/announce calls happen
"""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from fast_mcp_claude import config as config_mod
from fast_mcp_claude import launcher as L
from fast_mcp_claude.config import Settings

# Env vars _resolve_config consults; cleared per-test (mirrors test_channel.py).
_LAUNCHER_ENV = (
    "LAUNCHER_ENABLED",
    "CRM_IDENTITY",
    "CRM_LOCAL_URL",
    "CRM_POLL_S",
    "CRM_HEARTBEAT_S",
    "MCP_API_KEY",
)


@pytest.fixture
def env(monkeypatch):
    """monkeypatch with the launcher env vars removed (clean baseline)."""
    for name in _LAUNCHER_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def fake_settings(monkeypatch):
    """Install a controlled Settings so _resolve_config never reads the real .env."""

    def _install(**overrides) -> Settings:
        base = dict(
            peer_name="mini2",
            mcp_port=5499,
            mcp_api_key=None,
            mcp_auth_enabled=False,
            poll_max_wait_s=25,
            poll_heartbeat_s=20,
            launcher_enabled=False,
            launcher_cwd_allowlist="",
            launcher_tools_ceiling="",
            launcher_max_concurrent=2,
            launcher_task_timeout_s=900.0,
            launcher_reply_max_bytes=262144,
            launcher_setting_sources="",
            launcher_claude_bin="claude",
        )
        base.update(overrides)
        s = Settings(**base)
        monkeypatch.setattr(config_mod, "get_settings", lambda: s)
        return s

    return _install


def _cfg(**overrides) -> L.LauncherConfig:
    base = dict(
        identity="mini2_launcher",
        local_url="http://127.0.0.1:5499/mcp",
        api_key=None,
        poll=25.0,
        heartbeat=20.0,
        enabled=True,
        claude_bin="claude",
        cwd_allowlist=[],
        tools_ceiling=[],
        max_concurrent=2,
        task_timeout_s=900.0,
        reply_max_bytes=262144,
        setting_sources="",
        mcp_auth_enabled=True,
        mcp_api_key_present=True,
    )
    base.update(overrides)
    return L.LauncherConfig(**base)


# ---------------------------------------------------------------- fake client


class FakeClient:
    """Records reply()/announce() calls and serves canned tool results.

    Mimics the fastmcp.Client.call_tool surface enough for the reaper, reply
    sender, and inertness tests. call_tool returns an object with a `.data` dict
    (the path _result_data prefers).
    """

    class _Res:
        def __init__(self, data):
            self.data = data
            self.content = []

    def __init__(self, *, delivered=None, reply_ok=True):
        self.delivered = delivered or []
        self.reply_ok = reply_ok
        self.replies: list[dict] = []
        self.announces: list[dict] = []
        self.calls: list[str] = []

    async def call_tool(self, name, args):
        self.calls.append(name)
        if name == "list_messages":
            return self._Res({"success": True, "messages": list(self.delivered)})
        if name == "reply":
            self.replies.append(dict(args))
            return self._Res({"success": self.reply_ok})
        if name == "announce":
            self.announces.append(dict(args))
            return self._Res({"success": True})
        if name == "wait_for_instruction":
            return self._Res({"success": True, "message": None})
        return self._Res({"success": True})


# ---------------------------------------------------------------- fake claude bin


def _write_fake_claude(tmp_path: Path, body: str) -> str:
    """Write an executable python 'claude' that runs `body` and return its path."""
    p = tmp_path / "claude"
    p.write_text("#!/usr/bin/env python3\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


FAKE_CLAUDE_SUCCESS = """
import json, sys
print(json.dumps({
    "result": "done: 2+2=4",
    "session_id": "sess-abc",
    "total_cost_usd": 0.0123,
    "is_error": False,
    "num_turns": 3,
}))
sys.exit(0)
"""

FAKE_CLAUDE_NONZERO = """
import sys
sys.stderr.write("boom on stderr\\n")
sys.exit(7)
"""

FAKE_CLAUDE_SLEEP = """
import time
time.sleep(60)
"""


# ================================================================ config precedence


def test_disabled_by_default(env, fake_settings):
    fake_settings(launcher_enabled=False)
    assert L._resolve_config([]).enabled is False


def test_settings_enables(env, fake_settings):
    fake_settings(launcher_enabled=True)
    assert L._resolve_config([]).enabled is True


def test_env_enables_over_settings_off(env, fake_settings):
    fake_settings(launcher_enabled=False)
    env.setenv("LAUNCHER_ENABLED", "true")
    assert L._resolve_config([]).enabled is True


def test_env_can_disable_over_settings_on(env, fake_settings):
    fake_settings(launcher_enabled=True)
    env.setenv("LAUNCHER_ENABLED", "0")
    assert L._resolve_config([]).enabled is False


def test_cli_enabled_overrides_everything(env, fake_settings):
    fake_settings(launcher_enabled=False)
    env.setenv("LAUNCHER_ENABLED", "false")
    assert L._resolve_config(["--enabled"]).enabled is True


def test_cli_no_enabled_wins(env, fake_settings):
    fake_settings(launcher_enabled=True)
    env.setenv("LAUNCHER_ENABLED", "true")
    assert L._resolve_config(["--no-enabled"]).enabled is False


def test_identity_default_is_peer_name_launcher(env, fake_settings):
    fake_settings(peer_name="mini2")
    assert L._resolve_config([]).identity == "mini2_launcher"


def test_identity_env_beats_default(env, fake_settings):
    fake_settings(peer_name="mini2")
    env.setenv("CRM_IDENTITY", "env-id")
    assert L._resolve_config([]).identity == "env-id"


def test_identity_cli_beats_everything(env, fake_settings):
    fake_settings(peer_name="mini2")
    env.setenv("CRM_IDENTITY", "env-id")
    assert L._resolve_config(["--identity", "cli-id"]).identity == "cli-id"


# ================================================================ identity validation


def test_identity_valid_peer_name_launcher():
    assert L.validate_launcher_identity("mini2_launcher") == "mini2_launcher"


def test_identity_rejects_slash():
    # A peer_name with a slash would produce e.g. "a/b_launcher" — a dead mailbox.
    with pytest.raises(ValueError):
        L.validate_launcher_identity("a/b_launcher")


def test_identity_rejects_colon():
    with pytest.raises(ValueError):
        L.validate_launcher_identity("host:1_launcher")


async def test_serve_hard_fails_on_invalid_identity():
    # _serve must raise ValueError up to main() which exits non-zero.
    with pytest.raises(ValueError):
        await L._serve(_cfg(identity="bad/id_launcher", enabled=True))


# ================================================================ envelope parsing


def test_envelope_valid(tmp_path):
    cfg = _cfg(cwd_allowlist=[tmp_path.resolve()], tools_ceiling=["Read", "Grep"])
    env = L.parse_envelope(
        json.dumps({"task": "do x", "cwd": str(tmp_path), "allowed_tools": ["Read"]}), cfg
    )
    assert env.task == "do x"
    assert Path(env.cwd) == tmp_path.resolve()
    assert env.allowed_tools == ["Read"]
    assert env.timeout_s == cfg.task_timeout_s  # omitted -> cap


def test_envelope_malformed_json(tmp_path):
    cfg = _cfg(cwd_allowlist=[tmp_path.resolve()])
    with pytest.raises(L.EnvelopeError) as ei:
        L.parse_envelope("{not json", cfg)
    assert ei.value.payload["error"] == "bad_envelope"


def test_envelope_not_object(tmp_path):
    cfg = _cfg(cwd_allowlist=[tmp_path.resolve()])
    with pytest.raises(L.EnvelopeError) as ei:
        L.parse_envelope(json.dumps(["a", "b"]), cfg)
    assert ei.value.payload["error"] == "bad_envelope"


def test_envelope_missing_task(tmp_path):
    cfg = _cfg(cwd_allowlist=[tmp_path.resolve()])
    with pytest.raises(L.EnvelopeError) as ei:
        L.parse_envelope(json.dumps({"cwd": str(tmp_path)}), cfg)
    assert ei.value.payload["error"] == "bad_envelope"
    assert "task" in ei.value.payload["detail"]


def test_envelope_missing_cwd(tmp_path):
    cfg = _cfg(cwd_allowlist=[tmp_path.resolve()])
    with pytest.raises(L.EnvelopeError) as ei:
        L.parse_envelope(json.dumps({"task": "x"}), cfg)
    assert ei.value.payload["error"] == "bad_envelope"


def test_envelope_cwd_outside_allowlist(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    cfg = _cfg(cwd_allowlist=[allowed.resolve()])
    with pytest.raises(L.EnvelopeError) as ei:
        L.parse_envelope(json.dumps({"task": "x", "cwd": str(outside)}), cfg)
    assert ei.value.payload["error"] == "cwd_not_allowed"
    assert str(allowed.resolve()) in ei.value.payload["allowed"]


def test_envelope_cwd_empty_allowlist_rejects_all(tmp_path):
    cfg = _cfg(cwd_allowlist=[])
    with pytest.raises(L.EnvelopeError) as ei:
        L.parse_envelope(json.dumps({"task": "x", "cwd": str(tmp_path)}), cfg)
    assert ei.value.payload["error"] == "cwd_not_allowed"


def test_envelope_cwd_symlink_escape(tmp_path):
    """A symlink inside the allowlist pointing OUT must be rejected (realpath check)."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    link = allowed / "escape"
    link.symlink_to(outside)
    cfg = _cfg(cwd_allowlist=[allowed.resolve()])
    with pytest.raises(L.EnvelopeError) as ei:
        L.parse_envelope(json.dumps({"task": "x", "cwd": str(link)}), cfg)
    assert ei.value.payload["error"] == "cwd_not_allowed"


def test_envelope_tools_exceed_ceiling(tmp_path):
    cfg = _cfg(cwd_allowlist=[tmp_path.resolve()], tools_ceiling=["Read", "Grep"])
    with pytest.raises(L.EnvelopeError) as ei:
        L.parse_envelope(
            json.dumps({"task": "x", "cwd": str(tmp_path), "allowed_tools": ["Read", "Bash"]}),
            cfg,
        )
    assert ei.value.payload["error"] == "tools_exceed_ceiling"
    assert "Bash" in ei.value.payload["excess"]


def test_envelope_omitted_tools_uses_ceiling(tmp_path):
    cfg = _cfg(cwd_allowlist=[tmp_path.resolve()], tools_ceiling=["Read", "Grep"])
    env = L.parse_envelope(json.dumps({"task": "x", "cwd": str(tmp_path)}), cfg)
    assert env.allowed_tools == ["Read", "Grep"]


def test_envelope_timeout_capped(tmp_path):
    cfg = _cfg(cwd_allowlist=[tmp_path.resolve()], task_timeout_s=100.0)
    env = L.parse_envelope(
        json.dumps({"task": "x", "cwd": str(tmp_path), "timeout_s": 99999}), cfg
    )
    assert env.timeout_s == 100.0


def test_envelope_timeout_under_cap_kept(tmp_path):
    cfg = _cfg(cwd_allowlist=[tmp_path.resolve()], task_timeout_s=100.0)
    env = L.parse_envelope(json.dumps({"task": "x", "cwd": str(tmp_path), "timeout_s": 30}), cfg)
    assert env.timeout_s == 30.0


# ================================================================ reply truncation


def test_shape_reply_truncates_to_budget():
    big = "X" * 100_000
    out = L.shape_reply(
        ok=True,
        exit_code=0,
        timed_out=False,
        duration_s=1.0,
        result=big,
        stderr_tail="E" * 50_000,
        claude_session_id="s",
        cost_usd=0.01,
        is_error=False,
        num_turns=1,
        reply_max_bytes=2000,
    )
    assert len(out.encode("utf-8")) <= 2000
    obj = json.loads(out)
    assert obj["truncated"] is True
    assert obj["ok"] is True  # metadata fields survive truncation


def test_shape_reply_no_truncation_when_small():
    out = L.shape_reply(
        ok=True,
        exit_code=0,
        timed_out=False,
        duration_s=0.5,
        result="small",
        stderr_tail="",
        claude_session_id="s",
        cost_usd=None,
        is_error=False,
        num_turns=1,
        reply_max_bytes=262144,
    )
    obj = json.loads(out)
    assert obj["truncated"] is False
    assert obj["result"] == "small"


# ================================================================ parse claude json


def test_parse_claude_json_valid():
    raw = json.dumps(
        {"result": "hi", "session_id": "s1", "total_cost_usd": 0.02, "is_error": False,
         "num_turns": 2}
    )
    p = L.parse_claude_json(raw)
    assert p["result"] == "hi"
    assert p["session_id"] == "s1"
    assert p["total_cost_usd"] == 0.02
    assert p["is_error"] is False


def test_parse_claude_json_garbage_marks_error():
    p = L.parse_claude_json("not json at all")
    assert p["is_error"] is True
    assert p["session_id"] is None
    assert "not json" in p["result"]


# ================================================================ build cmd / ceiling


def _flag_value(cmd: list[str], flag: str):
    """Return the argument following `flag` in `cmd`, or KeyError-like None if absent."""
    if flag not in cmd:
        return None
    return cmd[cmd.index(flag) + 1]


def test_base_tool_names_strips_matchers_and_dedupes():
    # "Bash(uv run*)" -> "Bash"; "Read" -> "Read"; order-stable, deduped.
    assert L._base_tool_names(["Bash(uv run*)", "Read", "Bash(git*)", "Read"]) == ["Bash", "Read"]


def test_build_cmd_enforces_tools_ceiling_with_both_flags():
    """A non-empty ceiling produces --tools <base names> AND --allowedTools <full specs>.

    --tools is the actual restriction (tools not listed do not exist for the session);
    --allowedTools only auto-approves. Both must be present.
    """
    cfg = _cfg(setting_sources="")
    env = L.TaskEnvelope(
        task="do x", cwd="/tmp", allowed_tools=["Bash(uv run*)", "Read"], model=None, timeout_s=60.0
    )
    cmd = L._build_cmd(env, cfg)
    assert _flag_value(cmd, "--tools") == "Bash,Read"
    assert _flag_value(cmd, "--allowedTools") == "Bash(uv run*),Read"


def test_build_cmd_empty_ceiling_passes_empty_tools_no_allowedtools():
    """An empty ceiling => --tools "" (worker gets NO tools, pure reasoning) and NO
    --allowedTools flag at all."""
    cfg = _cfg(setting_sources="")
    env = L.TaskEnvelope(
        task="reason only", cwd="/tmp", allowed_tools=[], model=None, timeout_s=60.0
    )
    cmd = L._build_cmd(env, cfg)
    assert "--tools" in cmd
    assert _flag_value(cmd, "--tools") == ""
    assert "--allowedTools" not in cmd


def test_build_cmd_always_passes_setting_sources_even_when_empty():
    """--setting-sources is always present, including the empty string (load NO
    settings) — omitting it would load CLI defaults and let repo hooks bypass the
    ceiling."""
    cfg = _cfg(setting_sources="")
    env = L.TaskEnvelope(task="x", cwd="/tmp", allowed_tools=[], model=None, timeout_s=60.0)
    cmd = L._build_cmd(env, cfg)
    assert "--setting-sources" in cmd
    assert _flag_value(cmd, "--setting-sources") == ""


def test_build_cmd_always_passes_strict_mcp_config():
    """--strict-mcp-config is unconditional so a repo's .mcp.json can't hand the worker
    MCP servers/bearers."""
    cfg = _cfg()
    env = L.TaskEnvelope(task="x", cwd="/tmp", allowed_tools=[], model=None, timeout_s=60.0)
    assert "--strict-mcp-config" in L._build_cmd(env, cfg)


# ============================================================ approval hook (Phase 3)


def test_build_cmd_no_settings_flag_when_hook_disabled():
    """Default (Phase-2) posture: no approval hook => the argv carries NO --settings flag
    and --setting-sources stays "" (no repo settings, no hooks)."""
    cfg = _cfg(approval_hook_enabled=False)
    env = L.TaskEnvelope(task="x", cwd="/tmp", allowed_tools=["Read"], model=None, timeout_s=60.0)
    cmd = L._build_cmd(env, cfg)
    assert "--settings" not in cmd
    assert _flag_value(cmd, "--setting-sources") == ""


def test_build_cmd_no_settings_when_enabled_but_cmd_unresolved():
    """Fail-safe: enabled but the hook path didn't resolve => NO --settings injected (the
    _serve guard idles the launcher instead of spawning ungated workers)."""
    cfg = _cfg(approval_hook_enabled=True, approval_hook_cmd=None)
    env = L.TaskEnvelope(task="x", cwd="/tmp", allowed_tools=["Bash"], model=None, timeout_s=60.0)
    assert "--settings" not in L._build_cmd(env, cfg)


def test_build_cmd_arms_approval_hook_via_settings_not_setting_sources():
    """When armed, the hook rides on --settings (additive) while --setting-sources stays ""
    so a repo's .claude/settings.json is never loaded. The injected PreToolUse command carries
    the launcher-resolved hook path + the SOCKET PATH (not a secret) — never the mesh bearer."""
    cfg = _cfg(
        approval_hook_enabled=True,
        approval_hook_cmd="/abs/bin/fast-mcp-claude-hook",
        approval_auto_pass_tools="Read,Glob,Grep",
        approval_socket_path="/run/eca/launcher-approval.sock",
        api_key="secret-bearer",
        local_url="http://127.0.0.1:5499/mcp",
    )
    env = L.TaskEnvelope(
        task="x", cwd="/tmp", allowed_tools=["Bash(uv run*)"], model=None, timeout_s=60.0
    )
    cmd = L._build_cmd(env, cfg)
    # repo settings still NOT loaded
    assert _flag_value(cmd, "--setting-sources") == ""
    assert "--strict-mcp-config" in cmd
    settings = json.loads(_flag_value(cmd, "--settings"))
    pre = settings["hooks"]["PreToolUse"]
    assert len(pre) == 1 and pre[0]["matcher"] == "*"
    command = pre[0]["hooks"][0]["command"]
    assert command.endswith("/abs/bin/fast-mcp-claude-hook")
    assert "CRM_HOOK_SOCKET=/run/eca/launcher-approval.sock" in command
    assert "CRM_AUTO_PASS_TOOLS=Read,Glob,Grep" in command
    assert "CRM_DECISION_TIMEOUT=300" in command


def test_build_cmd_NEVER_puts_mesh_bearer_on_worker_argv():
    """REGRESSION (review finding #1): the mesh bearer must NEVER appear anywhere in the
    spawned worker's argv — only the launcher-owned socket path. A worker can read its own
    argv (same uid), so a leaked bearer would let it self-approve."""
    cfg = _cfg(
        approval_hook_enabled=True,
        approval_hook_cmd="/abs/bin/fast-mcp-claude-hook",
        approval_socket_path="/run/eca/launcher-approval.sock",
        api_key="SECRET-MESH-BEARER-9f8e",
        local_url="http://127.0.0.1:5499/mcp",
    )
    env = L.TaskEnvelope(task="x", cwd="/tmp", allowed_tools=["Bash"], model=None, timeout_s=60.0)
    joined = "\x00".join(L._build_cmd(env, cfg))
    assert "SECRET-MESH-BEARER-9f8e" not in joined
    assert "MCP_API_KEY" not in joined
    assert "CRM_LOCAL_URL" not in joined


def test_approval_hook_settings_shell_quotes_tricky_socket_path():
    """The socket path is shlex-quoted into the hook command so a path with metacharacters
    can't break out (defense in depth — values are launcher-controlled, not repo)."""
    cfg = _cfg(
        approval_hook_enabled=True,
        approval_hook_cmd="/abs/hook",
        approval_socket_path="/tmp/a b;rm -rf /.sock",
    )
    settings = json.loads(L._approval_hook_settings(cfg))
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "'/tmp/a b;rm -rf /.sock'" in command


def test_resolve_config_resolves_hook_path_when_enabled(env, fake_settings, monkeypatch):
    """_resolve_config resolves fast-mcp-claude-hook ONCE via shutil.which when the hook is
    enabled."""
    fake_settings(launcher_approval_hook_enabled=True, mcp_api_key="k", mcp_auth_enabled=True)
    monkeypatch.setattr(
        L.shutil, "which", lambda name: "/v/bin/fast-mcp-claude-hook" if "hook" in name else None
    )
    cfg = L._resolve_config([])
    assert cfg.approval_hook_enabled is True
    assert cfg.approval_hook_cmd == "/v/bin/fast-mcp-claude-hook"


def test_resolve_config_hook_cmd_none_when_unresolvable(env, fake_settings, monkeypatch):
    """If the hook can't be resolved, approval_hook_cmd stays None (the _serve guard then
    refuses to arm — fail-closed, never spawn ungated workers)."""
    fake_settings(launcher_approval_hook_enabled=True, mcp_api_key="k", mcp_auth_enabled=True)
    monkeypatch.setattr(L.shutil, "which", lambda name: None)
    cfg = L._resolve_config([])
    assert cfg.approval_hook_enabled is True
    assert cfg.approval_hook_cmd is None


# ---------------------------------------------------------------- approval relay


class _FakeReader:
    def __init__(self, data: bytes):
        self._data = data

    async def readline(self):
        return self._data


class _FakeWriter:
    def __init__(self):
        self.buf = b""
        self.closed = False

    def write(self, b):
        self.buf += b

    async def drain(self):
        pass

    def close(self):
        self.closed = True


async def test_relay_handler_relays_request_and_returns_decision(monkeypatch):
    captured = {}

    async def fake_relay(cfg, session_id, tool_name, tool_input):
        captured.update(session_id=session_id, tool_name=tool_name, tool_input=tool_input)
        return "allow", "approved by jeremy"

    monkeypatch.setattr(L, "_relay_decision", fake_relay)
    cfg = _cfg(approval_hook_enabled=True, approval_socket_path="/tmp/x.sock", api_key="k")
    req = json.dumps({"session_id": "s9", "tool_name": "Bash", "tool_input": {"command": "echo"}})
    writer = _FakeWriter()
    await L._handle_relay_conn(_FakeReader(req.encode() + b"\n"), writer, cfg, asyncio.Semaphore(2))
    assert captured == {"session_id": "s9", "tool_name": "Bash", "tool_input": {"command": "echo"}}
    assert json.loads(writer.buf.decode()) == {"decision": "allow", "reason": "approved by jeremy"}
    assert writer.closed is True


async def test_relay_handler_falls_back_to_ask_on_error(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("mesh down")

    monkeypatch.setattr(L, "_relay_decision", boom)
    cfg = _cfg(approval_hook_enabled=True, approval_socket_path="/tmp/x.sock", api_key="k")
    req = json.dumps({"session_id": "s", "tool_name": "Bash", "tool_input": {}})
    writer = _FakeWriter()
    await L._handle_relay_conn(_FakeReader(req.encode() + b"\n"), writer, cfg, asyncio.Semaphore(2))
    resp = json.loads(writer.buf.decode())
    assert resp["decision"] == "ask"
    assert "mesh down" in resp["reason"]


async def test_relay_socket_roundtrip_hook_to_launcher(monkeypatch):
    """End-to-end over a REAL unix socket: the worker hook (_relay_via_socket) talks to the
    launcher relay server, which relays a stubbed decision back. Exercises the actual socket
    plumbing of the security-critical path (worker gets no credential, only this socket)."""
    from fast_mcp_claude import hook as H

    # Short /tmp path: a unix socket path must stay under the platform cap (~104 on macOS),
    # which pytest's tmp_path exceeds.
    sock = f"/tmp/eca_relay_rt_{os.getpid()}.sock"

    async def fake_relay(cfg, session_id, tool_name, tool_input):
        assert (session_id, tool_name) == ("s1", "Bash")
        return ("deny", "nope from operator")

    monkeypatch.setattr(L, "_relay_decision", fake_relay)
    cfg = _cfg(approval_hook_enabled=True, approval_socket_path=sock, api_key="k", max_concurrent=2)
    stop = asyncio.Event()
    server_task = asyncio.create_task(_approval_relay_server_with_sem(cfg, stop))
    try:
        async with asyncio.timeout(3):
            while not os.path.exists(sock):
                await asyncio.sleep(0.01)
        decision, reason = await H._relay_via_socket(sock, "s1", "Bash", {"command": "echo"}, 5.0)
        assert decision == "deny"
        assert reason == "nope from operator"
    finally:
        stop.set()
        await server_task


async def _approval_relay_server_with_sem(cfg, stop):
    await L._approval_relay_server(cfg, stop, asyncio.Semaphore(cfg.max_concurrent))


# ================================================================ scrubbed env


def test_scrubbed_env_drops_mesh_secrets(monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", "secret-bearer")
    monkeypatch.setenv("CRM_LOCAL_URL", "http://x")
    monkeypatch.setenv("HOME", "/home/me")
    monkeypatch.setenv("PATH", "/usr/bin")
    scrubbed = L._scrubbed_env()
    assert "MCP_API_KEY" not in scrubbed
    assert "CRM_LOCAL_URL" not in scrubbed
    assert scrubbed["HOME"] == "/home/me"
    assert scrubbed["PATH"] == "/usr/bin"


# ================================================================ reaper


async def test_reaper_replies_own_identity_only():
    client = FakeClient(
        delivered=[
            {"id": "a" * 32, "recipient_session": "mini2_launcher"},
            {"id": "b" * 32, "recipient_session": "other_launcher"},
            {"id": "c" * 32, "recipient_session": "mini2_launcher"},
        ]
    )
    await L._reap_stale_claims(client, _cfg(identity="mini2_launcher"))
    reaped_ids = {r["message_id"] for r in client.replies}
    assert reaped_ids == {"a" * 32, "c" * 32}
    for r in client.replies:
        assert json.loads(r["response"])["error"] == "launcher_restarted_task_lost"


async def test_reaper_noop_when_none_match():
    client = FakeClient(delivered=[{"id": "b" * 32, "recipient_session": "other_launcher"}])
    await L._reap_stale_claims(client, _cfg(identity="mini2_launcher"))
    assert client.replies == []


async def test_reaper_skips_live_inflight_but_reaps_genuine_orphan():
    """On a reconnect, a delivered row whose id is currently in-flight in THIS process
    must NOT be reaped (reaping it would fail the running task), while a genuinely
    orphaned delivered row (no live id) IS reaped."""
    live_id = "a" * 32  # a task we are actively running right now
    orphan_id = "c" * 32  # left over from a previous crash — no live task
    client = FakeClient(
        delivered=[
            {"id": live_id, "recipient_session": "mini2_launcher"},
            {"id": orphan_id, "recipient_session": "mini2_launcher"},
        ]
    )
    await L._reap_stale_claims(client, _cfg(identity="mini2_launcher"), inflight={live_id})
    reaped_ids = {r["message_id"] for r in client.replies}
    assert reaped_ids == {orphan_id}  # the orphan is reaped
    assert live_id not in reaped_ids  # the live in-flight task is spared


# ================================================================ always-reply


async def test_handler_internal_error_still_replies(monkeypatch, tmp_path):
    cfg = _cfg(cwd_allowlist=[tmp_path.resolve()], tools_ceiling=["Read"])

    async def boom(env, cfg, live):
        raise RuntimeError("kaboom mid-spawn")

    monkeypatch.setattr(L, "_run_claude", boom)
    client = FakeClient()
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    msg = {"id": "d" * 32, "prompt": json.dumps({"task": "x", "cwd": str(tmp_path)})}
    await L._handle_task(client, msg, cfg, sem, set(), L._Counter(), set())
    # spawn_failed (the except Exception around _run_claude) — exactly one reply.
    assert len(client.replies) == 1
    payload = json.loads(client.replies[0]["response"])
    assert payload["error"] == "spawn_failed"
    assert sem._value == 1  # slot released


async def test_handler_truly_internal_error_replies_launcher_internal(monkeypatch, tmp_path):
    """If even envelope parsing path raises unexpectedly, the outer wrap posts
    launcher_internal. Force parse_envelope to raise a non-EnvelopeError."""
    cfg = _cfg(cwd_allowlist=[tmp_path.resolve()])

    def explode(prompt, cfg):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(L, "parse_envelope", explode)
    client = FakeClient()
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    msg = {"id": "e" * 32, "prompt": "{}"}
    await L._handle_task(client, msg, cfg, sem, set(), L._Counter(), set())
    assert len(client.replies) == 1
    assert json.loads(client.replies[0]["response"])["error"] == "launcher_internal"


# ================================================================ fake claude spawn


async def test_spawn_success(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = _write_fake_claude(bin_dir, FAKE_CLAUDE_SUCCESS)
    cfg = _cfg(cwd_allowlist=[cwd.resolve()], tools_ceiling=["Read"], claude_bin=claude)
    client = FakeClient()
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    msg = {"id": "f" * 32, "prompt": json.dumps({"task": "what is 2+2", "cwd": str(cwd)})}
    await L._handle_task(client, msg, cfg, sem, set(), L._Counter(), set())
    assert len(client.replies) == 1
    r = json.loads(client.replies[0]["response"])
    assert r["ok"] is True
    assert r["exit_code"] == 0
    assert r["timed_out"] is False
    assert r["claude_session_id"] == "sess-abc"
    assert r["cost_usd"] == 0.0123
    assert r["num_turns"] == 3
    assert "2+2=4" in r["result"]


async def test_spawn_nonzero_exit(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = _write_fake_claude(bin_dir, FAKE_CLAUDE_NONZERO)
    cfg = _cfg(cwd_allowlist=[cwd.resolve()], claude_bin=claude)
    client = FakeClient()
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    msg = {"id": "0" * 32, "prompt": json.dumps({"task": "fail", "cwd": str(cwd)})}
    await L._handle_task(client, msg, cfg, sem, set(), L._Counter(), set())
    r = json.loads(client.replies[0]["response"])
    assert r["ok"] is False
    assert r["exit_code"] == 7
    assert r["timed_out"] is False
    assert "boom on stderr" in r["stderr_tail"]


async def test_spawn_timeout_kills_group(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = _write_fake_claude(bin_dir, FAKE_CLAUDE_SLEEP)
    cfg = _cfg(cwd_allowlist=[cwd.resolve()], claude_bin=claude)
    # Shrink the kill grace so the test is fast.
    import fast_mcp_claude.launcher as mod

    old_grace = mod.KILL_GRACE_S
    mod.KILL_GRACE_S = 0.5
    try:
        env = L.TaskEnvelope(task="sleep", cwd=str(cwd), allowed_tools=[], model=None,
                             timeout_s=0.5)
        live: set = set()
        run = await L._run_claude(env, cfg, live)
    finally:
        mod.KILL_GRACE_S = old_grace
    assert run.timed_out is True
    assert run.exit_code is not None  # process reaped (killed)
    assert live == set()  # cleaned up


async def test_spawn_missing_binary_replies_spawn_failed(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    cfg = _cfg(cwd_allowlist=[cwd.resolve()], claude_bin=str(tmp_path / "nope" / "claude"))
    client = FakeClient()
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    msg = {"id": "1" * 32, "prompt": json.dumps({"task": "x", "cwd": str(cwd)})}
    await L._handle_task(client, msg, cfg, sem, set(), L._Counter(), set())
    r = json.loads(client.replies[0]["response"])
    assert r["error"] == "spawn_failed"


# ================================================================ disabled inertness


async def test_disabled_never_polls(monkeypatch):
    """Disabled => _serve must idle (never construct a client / call any tool)."""
    started = {"idle": False, "preflight": False}

    async def fake_idle(reason):
        started["idle"] = True  # reached the inert path

    async def fake_preflight(cfg):
        started["preflight"] = True
        return L.Preflight(ok=True, bin_path="/x/claude", version="2")

    monkeypatch.setattr(L, "_idle_forever", fake_idle)
    monkeypatch.setattr(L, "_preflight", fake_preflight)
    await L._serve(_cfg(enabled=False))
    assert started["idle"] is True
    # The preflight must NOT run when disabled (we idle before any binary probe).
    assert started["preflight"] is False


async def test_missing_binary_idles_not_polls(monkeypatch):
    """Enabled but binary missing => disabled-with-error: idle, never poll."""
    idled = {"reason": None}
    bridged = {"called": False}

    async def fake_idle(reason):
        idled["reason"] = reason

    async def fake_bridge(cfg):
        bridged["called"] = True

    async def fake_preflight(cfg):
        return L.Preflight(ok=False, reason="claude binary not found")

    monkeypatch.setattr(L, "_idle_forever", fake_idle)
    monkeypatch.setattr(L, "_bridge", fake_bridge)
    monkeypatch.setattr(L, "_preflight", fake_preflight)
    await L._serve(_cfg(enabled=True))
    assert bridged["called"] is False
    assert "DISABLED-WITH-ERROR" in idled["reason"]


async def test_unauthenticated_mesh_refuses_to_arm(monkeypatch):
    """Enabled but the local mesh is effectively unauthenticated (auth disabled OR no
    api key) => refuse to arm: idle, never preflight, never bridge. A spawned worker on
    localhost could otherwise spoof reply/send_prompt."""
    for posture in (
        {"mcp_auth_enabled": False, "mcp_api_key_present": True},
        {"mcp_auth_enabled": True, "mcp_api_key_present": False},
    ):
        idled = {"reason": None}
        bridged = {"called": False}
        preflighted = {"called": False}

        async def fake_idle(reason):
            idled["reason"] = reason

        async def fake_bridge(cfg):
            bridged["called"] = True

        async def fake_preflight(cfg):
            preflighted["called"] = True
            return L.Preflight(ok=True, bin_path="/x/claude", version="2")

        monkeypatch.setattr(L, "_idle_forever", fake_idle)
        monkeypatch.setattr(L, "_bridge", fake_bridge)
        monkeypatch.setattr(L, "_preflight", fake_preflight)
        await L._serve(_cfg(enabled=True, **posture))
        assert bridged["called"] is False, posture
        assert preflighted["called"] is False, posture  # guard fires before preflight
        assert "REFUSING TO ARM" in idled["reason"], posture


async def test_authenticated_mesh_proceeds_to_preflight(monkeypatch):
    """The happy posture (auth on AND api key present) passes the guard and reaches
    preflight."""
    preflighted = {"called": False}

    async def fake_preflight(cfg):
        preflighted["called"] = True
        return L.Preflight(ok=False, reason="stop here, binary missing")

    async def fake_idle(reason):
        pass

    monkeypatch.setattr(L, "_preflight", fake_preflight)
    monkeypatch.setattr(L, "_idle_forever", fake_idle)
    await L._serve(_cfg(enabled=True, mcp_auth_enabled=True, mcp_api_key_present=True))
    assert preflighted["called"] is True


# ================================================================ heartbeat metadata


def test_announce_metadata_shape():
    cfg = _cfg(cwd_allowlist=[Path("/tmp/a")], tools_ceiling=["Read", "Grep"], max_concurrent=3)
    md = L._announce_metadata(cfg)
    assert md["role"] == "launcher"
    assert md["max_concurrent"] == 3
    assert md["tools_ceiling"] == ["Read", "Grep"]
    assert md["cwd_allowlist"] == ["/tmp/a"]


# ================================================================ GH #3 reconnect

# A `fast-mcp-claude` restart kills the launcher's MCP session; the heartbeat must detect the
# persistent failure and force a client rebuild instead of announce-failing forever (and the
# blocked long-poll must abort rather than strand on the dead session).


class _AnnounceRaises:
    """Local client whose announce() always raises; everything else is unexpected."""

    def __init__(self, exc):
        self._exc = exc
        self.announce_calls = 0

    async def call_tool(self, name, args):
        if name == "announce":
            self.announce_calls += 1
            raise self._exc
        raise AssertionError(f"unexpected call {name!r}")


async def test_heartbeat_trips_reconnect_after_consecutive_failures():
    client = _AnnounceRaises(RuntimeError("Session terminated"))
    ev = asyncio.Event()
    await asyncio.wait_for(
        L._heartbeat_loop(client, _cfg(heartbeat=0.0), L._Counter(), ev), timeout=2.0
    )
    assert ev.is_set()  # signalled the bridge to rebuild
    assert client.announce_calls == L._ANNOUNCE_FAILS_BEFORE_RECONNECT


async def test_heartbeat_auth_failure_never_trips_reconnect():
    """A bad bearer won't be fixed by reconnecting (and could re-arm the 60s lockout), so an
    auth error must keep retrying without ever signalling a rebuild."""
    client = _AnnounceRaises(RuntimeError("401 Unauthorized"))
    ev = asyncio.Event()
    task = asyncio.create_task(L._heartbeat_loop(client, _cfg(heartbeat=0.0), L._Counter(), ev))
    await asyncio.sleep(0.05)  # let it fail many times
    assert not ev.is_set()
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.announce_calls > L._ANNOUNCE_FAILS_BEFORE_RECONNECT  # kept trying, never tripped


async def test_heartbeat_transient_blip_does_not_accumulate():
    """One failure among successes resets the counter — only a SUSTAINED outage rebuilds."""

    class _FlakyOnce:
        def __init__(self):
            self.n = 0

        async def call_tool(self, name, args):
            self.n += 1
            if name == "announce" and self.n == 1:
                raise RuntimeError("one-off blip")
            return FakeClient._Res({"success": True})

    ev = asyncio.Event()
    task = asyncio.create_task(
        L._heartbeat_loop(_FlakyOnce(), _cfg(heartbeat=0.0), L._Counter(), ev)
    )
    await asyncio.sleep(0.05)
    assert not ev.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_wait_for_instruction_aborts_when_session_dies():
    """A long-poll blocked against a dead session aborts with _ReconnectNeeded once the heartbeat
    flags it — the core of the fix: no more stranding forever (GH #3)."""
    never = asyncio.Event()

    class _Blocks:
        async def call_tool(self, name, args):
            assert name == "wait_for_instruction"
            await never.wait()  # a poll that never returns against the dead session

    ev = asyncio.Event()
    task = asyncio.create_task(L._wait_for_instruction_or_reconnect(_Blocks(), _cfg(poll=30.0), ev))
    await asyncio.sleep(0.01)
    assert not task.done()  # genuinely blocked on the poll
    ev.set()  # heartbeat detected the dead session
    with pytest.raises(L._ReconnectNeeded):
        await asyncio.wait_for(task, timeout=2.0)


async def test_wait_for_instruction_returns_result_when_healthy():
    class _Returns:
        async def call_tool(self, name, args):
            return FakeClient._Res({"success": True, "message": {"id": "x" * 32}})

    ev = asyncio.Event()
    res = await L._wait_for_instruction_or_reconnect(_Returns(), _cfg(poll=1.0), ev)
    assert L._result_data(res)["message"]["id"] == "x" * 32
    assert not ev.is_set()


# Reference os/sys so static checkers don't flag the imports used only in fakes.
_ = (os, sys)
