"""Tests for the fast-mcp-claude-channel adapter.

Focus: the strict-opt-in `enabled` gate and the identity/summary precedence in
`_resolve_config` (CLI flag > env var > Settings default), plus the key safety
behavior in `_serve` — a disabled adapter completes the MCP handshake but never
starts the inbox-polling bridge, so a wired-but-unintended channel entry can't
claim messages out from under /worker loop mode.

The live two-session push path still requires launching a real worker with
`--dangerously-load-development-channels` and is exercised manually.
"""

import json
from pathlib import Path

import anyio
import pytest

from fast_mcp_claude import channel as channel_mod
from fast_mcp_claude import config as config_mod
from fast_mcp_claude.config import Settings

# Env vars _resolve_config consults; cleared per-test so ambient shell/.env state
# can't leak into the precedence assertions.
_CHANNEL_ENV = (
    "CHANNEL_ENABLED",
    "CRM_IDENTITY",
    "CRM_SUMMARY",
    "CRM_LOCAL_URL",
    "CRM_POLL_S",
    "CRM_HEARTBEAT_S",
    "MCP_API_KEY",
    "CHANNEL_DECISION_TIMEOUT_S",
    "CHANNEL_REPLY_TIMEOUT_S",
    "CHANNEL_AUTO_PASS_TOOLS",
    "CRM_SESSION_STATUS_FILE",
    "CHANNEL_LIVENESS_CHECK",
    "CHANNEL_LIVENESS_WINDOW_S",
)


@pytest.fixture
def env(monkeypatch):
    """monkeypatch with the channel env vars removed (clean baseline)."""
    for name in _CHANNEL_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def fake_settings(monkeypatch):
    """Install a controlled Settings so _resolve_config never reads the real .env."""

    def _install(**overrides) -> Settings:
        base = dict(
            peer_name="peer-default",
            mcp_port=5499,
            mcp_api_key=None,
            mcp_auth_enabled=False,
            poll_max_wait_s=25,
            poll_heartbeat_s=20,
            channel_enabled=False,
            channel_identity=None,
            channel_summary=None,
        )
        base.update(overrides)
        s = Settings(**base)
        monkeypatch.setattr(config_mod, "get_settings", lambda: s)
        return s

    return _install


def _cfg(**overrides) -> channel_mod.ChannelConfig:
    base = dict(
        identity="x",
        local_url="http://127.0.0.1:5499/mcp",
        api_key=None,
        summary=None,
        poll=25.0,
        heartbeat=20.0,
        enabled=False,
    )
    base.update(overrides)
    return channel_mod.ChannelConfig(**base)


# --------------------------------------------------------------- enabled gate


def test_disabled_by_default(env, fake_settings):
    fake_settings(channel_enabled=False)
    assert channel_mod._resolve_config([]).enabled is False


def test_settings_enables_bridge(env, fake_settings):
    fake_settings(channel_enabled=True)
    assert channel_mod._resolve_config([]).enabled is True


def test_env_enables_over_settings_off(env, fake_settings):
    fake_settings(channel_enabled=False)
    env.setenv("CHANNEL_ENABLED", "true")
    assert channel_mod._resolve_config([]).enabled is True


def test_env_can_disable_over_settings_on(env, fake_settings):
    fake_settings(channel_enabled=True)
    env.setenv("CHANNEL_ENABLED", "0")
    assert channel_mod._resolve_config([]).enabled is False


def test_cli_enabled_overrides_everything(env, fake_settings):
    fake_settings(channel_enabled=False)
    env.setenv("CHANNEL_ENABLED", "false")
    assert channel_mod._resolve_config(["--enabled"]).enabled is True


def test_cli_no_enabled_wins_over_env_and_settings(env, fake_settings):
    # Safety case: even with env AND Settings both ON, an explicit --no-enabled wins.
    fake_settings(channel_enabled=True)
    env.setenv("CHANNEL_ENABLED", "true")
    assert channel_mod._resolve_config(["--no-enabled"]).enabled is False


def test_liveness_check_enabled_default_is_true():
    # ECA-71: the fast non-consumption bounce is DEFAULT-ON. The fast-path tests set
    # liveness_check_enabled explicitly on ChannelConfig, so an accidental revert of the Settings
    # default to False would leave them all green — this pins the source of the default directly.
    assert Settings.model_fields["channel_liveness_check_enabled"].default is True


def test_liveness_check_env_can_disable_default(env, fake_settings):
    # Per-host opt-out documented in config.py: CHANNEL_LIVENESS_CHECK=0 overrides the default-on.
    fake_settings()
    env.setenv("CHANNEL_LIVENESS_CHECK", "0")
    assert channel_mod._resolve_config([]).liveness_check_enabled is False


# ----------------------------------------------------------- identity precedence


def test_identity_falls_back_to_peer_name(env, fake_settings):
    fake_settings(peer_name="pn", channel_identity=None)
    assert channel_mod._resolve_config([]).identity == "pn"


def test_identity_settings_channel_beats_peer_name(env, fake_settings):
    fake_settings(peer_name="pn", channel_identity="chan-id")
    assert channel_mod._resolve_config([]).identity == "chan-id"


def test_identity_env_beats_settings(env, fake_settings):
    fake_settings(peer_name="pn", channel_identity="chan-id")
    env.setenv("CRM_IDENTITY", "env-id")
    assert channel_mod._resolve_config([]).identity == "env-id"


def test_identity_cli_beats_everything(env, fake_settings):
    fake_settings(peer_name="pn", channel_identity="chan-id")
    env.setenv("CRM_IDENTITY", "env-id")
    assert channel_mod._resolve_config(["--identity", "cli-id"]).identity == "cli-id"


# ------------------------------------------------------------ summary precedence


def test_summary_from_settings(env, fake_settings):
    fake_settings(channel_summary="reviewing PRs")
    assert channel_mod._resolve_config([]).summary == "reviewing PRs"


def test_summary_cli_beats_settings(env, fake_settings):
    fake_settings(channel_summary="reviewing PRs")
    assert channel_mod._resolve_config(["--summary", "cli blurb"]).summary == "cli blurb"


# ---------------------------------------------------- _serve inertness (safety)


class _DummyStreams:
    """Async context manager standing in for stdio_server()."""

    async def __aenter__(self):
        return (object(), object())

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def patched_serve(monkeypatch):
    """Stub the stdio transport + server.run + the background loops, and count their starts.

    The bridge is now three concurrent loops (presence heartbeat, inbox push, permission-relay
    tee) rather than one `_bridge`; the safety invariant is unchanged: a disabled adapter starts
    NONE of them (handshake only), an enabled one starts all three."""
    calls = {"presence": 0, "inbox": 0, "tee": 0}

    async def fake_presence(cfg):
        calls["presence"] += 1
        await anyio.sleep_forever()

    async def fake_inbox(cfg, write_stream):
        calls["inbox"] += 1
        await anyio.sleep_forever()

    async def fake_tee(read_stream, dst, write_stream, cfg, tg):
        calls["tee"] += 1
        await anyio.sleep_forever()

    async def fake_run(read_stream, write_stream, init_options):
        # Yield long enough that the started child tasks get a slot, then return as if
        # stdin closed and the session ended.
        await anyio.sleep(0.05)

    monkeypatch.setattr(channel_mod, "_presence_loop", fake_presence)
    monkeypatch.setattr(channel_mod, "_inbox_loop", fake_inbox)
    monkeypatch.setattr(channel_mod, "_tee_reader", fake_tee)
    monkeypatch.setattr(channel_mod, "stdio_server", lambda: _DummyStreams())
    monkeypatch.setattr(channel_mod._server, "run", fake_run)
    return calls


def test_serve_disabled_never_starts_bridge(patched_serve):
    # The core invariant: disabled => handshake only, no polling/claiming/pushing/relaying.
    anyio.run(channel_mod._serve, _cfg(enabled=False))
    assert patched_serve == {"presence": 0, "inbox": 0, "tee": 0}


def test_serve_enabled_starts_bridge(patched_serve):
    anyio.run(channel_mod._serve, _cfg(enabled=True))
    assert patched_serve == {"presence": 1, "inbox": 1, "tee": 1}


# ------------------------------------------------------- ECA-82: graceful forget wiring in _serve


def test_serve_calls_graceful_forget_after_server_run(monkeypatch, patched_serve):
    """_graceful_forget must run once _server.run returns (clean exit) -- verifies the wiring,
    not _graceful_forget's own internal logic (covered separately below). The real call sits
    inside a shielded scope (see _serve), so a real await checkpoint here still must complete
    without raising -- if a regression ever dropped the shield, this would surface as a
    propagated CancelledError instead of a silent pass."""
    calls = []

    async def fake_graceful_forget(cfg):
        await anyio.sleep(0)
        calls.append(cfg)

    monkeypatch.setattr(channel_mod, "_graceful_forget", fake_graceful_forget)
    anyio.run(channel_mod._serve, _cfg(enabled=True))
    assert len(calls) == 1


def test_serve_cancels_other_loops_before_calling_graceful_forget(monkeypatch, patched_serve):
    """Regression guard for an adversarial-review finding on this branch: the ORIGINAL wiring
    called `_graceful_forget` BEFORE cancelling the task group, leaving `_presence_loop` free to
    re-announce with our own (still-valid) token and resurrect the very row the forget just
    deleted -- reopening the exact claim-gap AC1 exists to close. The fix cancels first (so
    `_presence_loop` can no longer start another announce) and only then runs the shielded
    forget. `fake_presence` records whether IT observed cancellation before `fake_graceful_forget`
    ran; both stubs yield at least once so the event loop actually gets to interleave them."""
    order = []

    async def fake_presence(cfg):
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            order.append("presence_cancelled")
            raise

    async def fake_graceful_forget(cfg):
        await anyio.sleep(0)  # yield so an already-scheduled cancellation gets to land first
        order.append("graceful_forget")

    monkeypatch.setattr(channel_mod, "_presence_loop", fake_presence)
    monkeypatch.setattr(channel_mod, "_graceful_forget", fake_graceful_forget)
    anyio.run(channel_mod._serve, _cfg(enabled=True))
    assert order == ["presence_cancelled", "graceful_forget"]


def test_serve_disabled_never_calls_graceful_forget(monkeypatch, patched_serve):
    # The disabled path returns from its own inert handshake before the bridge (and this call)
    # are ever reached.
    calls = []

    async def fake_graceful_forget(cfg):
        calls.append(cfg)

    monkeypatch.setattr(channel_mod, "_graceful_forget", fake_graceful_forget)
    anyio.run(channel_mod._serve, _cfg(enabled=False))
    assert calls == []


# ============================================================ two-way + permission relay (v1)


# -------------------------------------------------- new config fields precedence


def test_decision_timeout_default(env, fake_settings):
    fake_settings()
    assert channel_mod._resolve_config([]).decision_timeout == 300.0


def test_decision_timeout_cli_beats_env(env, fake_settings):
    fake_settings()
    env.setenv("CHANNEL_DECISION_TIMEOUT_S", "120")
    assert channel_mod._resolve_config(["--decision-timeout", "45"]).decision_timeout == 45.0


def test_reply_timeout_env(env, fake_settings):
    fake_settings()
    env.setenv("CHANNEL_REPLY_TIMEOUT_S", "600")
    assert channel_mod._resolve_config([]).reply_timeout == 600.0


def test_auto_pass_default_is_read_only(env, fake_settings):
    fake_settings()
    assert channel_mod._resolve_config([]).auto_pass_tools == frozenset({"Read", "Glob", "Grep"})


def test_auto_pass_cli_overrides(env, fake_settings):
    fake_settings()
    cfg = channel_mod._resolve_config(["--auto-pass", "Read, Grep , LS"])
    assert cfg.auto_pass_tools == frozenset({"Read", "Grep", "LS"})


def test_auto_pass_empty_cli_opts_out(env, fake_settings):
    # `--auto-pass ""` must yield an EMPTY set (every tool routes), NOT silently fall through to
    # the env/Settings default — the frozenset()-is-falsy `or` bug. Env must not leak in either.
    fake_settings()
    env.setenv("CHANNEL_AUTO_PASS_TOOLS", "Read,Glob,Grep")
    assert channel_mod._resolve_config(["--auto-pass", ""]).auto_pass_tools == frozenset()


def test_status_file_from_env(env, fake_settings):
    fake_settings()
    env.setenv("CRM_SESSION_STATUS_FILE", "/tmp/x.json")
    assert channel_mod._resolve_config([]).status_file == "/tmp/x.json"


# -------------------------------------------------- _is_auto_pass


def test_auto_pass_our_reply_tool_always():
    cfg = _cfg(auto_pass_tools=frozenset())  # even with NO read-only allowlist
    assert channel_mod._is_auto_pass(channel_mod.OUR_REPLY_TOOL, cfg) is True


def test_auto_pass_read_only_yes_consequential_no():
    cfg = _cfg(auto_pass_tools=frozenset({"Read", "Glob", "Grep"}))
    assert channel_mod._is_auto_pass("Read", cfg) is True
    assert channel_mod._is_auto_pass("Bash", cfg) is False
    assert channel_mod._is_auto_pass("Write", cfg) is False


# -------------------------------------------------- SERVER_NAME doc/config consistency (FMC-8)
#
# Claude Code names MCP tools mcp__<.mcp.json config key>__<tool>, not by the server's
# self-declared MCP initialize name — so OUR_REPLY_TOOL et al. (built from the hardcoded
# SERVER_NAME) only match the real tool name when every documented/example config key
# equals SERVER_NAME. These guard against that drifting apart again.

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_mcp_json_example_channel_key_matches_server_name():
    config = json.loads((_REPO_ROOT / ".mcp.json.example").read_text())
    assert channel_mod.SERVER_NAME in config["mcpServers"]


def test_readme_dev_channels_flag_matches_server_name():
    readme = (_REPO_ROOT / "README.md").read_text()
    assert f"server:{channel_mod.SERVER_NAME}" in readme


def test_worker_command_dev_channels_flag_matches_server_name():
    worker_md = (_REPO_ROOT / ".claude" / "commands" / "worker.md").read_text()
    assert f"server:{channel_mod.SERVER_NAME}" in worker_md


# -------------------------------------------------- _build_presence (subsumes session.py)


def test_presence_stamps_role_and_channel_no_status_file():
    cfg = _cfg(identity="mini2.repo", status_file=None)
    summary, meta = channel_mod._build_presence(cfg)
    assert meta["role"] == "live-session"
    assert meta["channel"] is True
    assert "mini2.repo" in summary


def test_presence_reads_status_file(tmp_path):
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({
        "machine": "mini2", "repo": "evolv-coder-agent", "branch": "dev",
        "status": "active", "last": "writing tests", "cwd": "/x",
    }))
    cfg = _cfg(identity="mini2.eca", status_file=str(sf))
    summary, meta = channel_mod._build_presence(cfg)
    assert meta["role"] == "live-session" and meta["channel"] is True
    assert meta["repo"] == "evolv-coder-agent" and meta["branch"] == "dev"
    assert "writing tests" in summary


def test_presence_includes_name(tmp_path):
    # ADR-0016: presence publishes the session name + folds it into the summary.
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({
        "machine": "mini2", "repo": "evolv-coder-agent", "name": "planning",
        "branch": "dev", "status": "active",
    }))
    cfg = _cfg(identity="mini2.eca", status_file=str(sf))
    summary, meta = channel_mod._build_presence(cfg)
    assert meta["name"] == "planning"
    assert "planning" in summary


def test_presence_includes_description_and_claude_session_id(tmp_path):
    # ADR-0016 Amendment 1 (ECA-23): both fields ride the same status-file pipeline as name.
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({
        "machine": "mini2", "repo": "evolv-coder-agent",
        "session_description": "fixing the auth bug", "claude_session_id": "abc-123",
        "branch": "dev", "status": "active",
    }))
    cfg = _cfg(identity="mini2.eca", status_file=str(sf))
    _, meta = channel_mod._build_presence(cfg)
    assert meta["session_description"] == "fixing the auth bug"
    assert meta["claude_session_id"] == "abc-123"


def test_presence_omits_description_and_claude_session_id_when_absent(tmp_path):
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"machine": "mini2", "repo": "evolv-coder-agent", "status": "active"}))
    cfg = _cfg(identity="mini2.eca", status_file=str(sf))
    _, meta = channel_mod._build_presence(cfg)
    assert "session_description" not in meta
    assert "claude_session_id" not in meta


def test_presence_includes_context_and_cost_telemetry(tmp_path):
    # ECA-49: statusline_hook.py's fields + session_hook.py's message_count, same pipeline.
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({
        "machine": "mini2", "repo": "evolv-coder-agent", "status": "active",
        "context_pct": 42, "context_tokens_used": 84000, "context_window_size": 200000,
        "cost_usd": 1.23, "message_count": 7, "started_at": 1700000000.0,
    }))
    cfg = _cfg(identity="mini2.eca", status_file=str(sf))
    _, meta = channel_mod._build_presence(cfg)
    assert meta["context_pct"] == 42
    assert meta["context_tokens_used"] == 84000
    assert meta["context_window_size"] == 200000
    assert meta["cost_usd"] == 1.23
    assert meta["message_count"] == 7
    assert meta["session_started_at"] == 1700000000.0


def test_presence_omits_context_and_cost_telemetry_when_absent(tmp_path):
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"machine": "mini2", "repo": "evolv-coder-agent", "status": "active"}))
    cfg = _cfg(identity="mini2.eca", status_file=str(sf))
    _, meta = channel_mod._build_presence(cfg)
    for key in ("context_pct", "context_tokens_used", "context_window_size", "cost_usd",
                "message_count", "session_started_at"):
        assert key not in meta


# -------------------------------------------------- permission relay routing


@pytest.fixture
def relay(monkeypatch):
    """Record _send_permission verdicts and stub _route_approval; manage the _RT global."""
    sent = []  # list of (request_id, behavior)
    routed = []  # list of inflight dicts passed to _route_approval

    async def fake_send(write_stream, request_id, behavior):
        sent.append((request_id, behavior))

    async def fake_route(cfg, inflight, tool_name, description, preview):
        routed.append({"inflight": inflight, "tool_name": tool_name})
        return "deny"  # configured verdict from the (mocked) Teams round-trip

    monkeypatch.setattr(channel_mod, "_send_permission", fake_send)
    monkeypatch.setattr(channel_mod, "_route_approval", fake_route)

    def _set_inflight(inflight):
        channel_mod._RT = channel_mod._Runtime(cfg=_cfg(enabled=True))
        channel_mod._RT.inflight = inflight

    yield {"sent": sent, "routed": routed, "set_inflight": _set_inflight}
    channel_mod._RT = None


def _perm_params(tool_name, request_id="vaxrc"):
    return {"request_id": request_id, "tool_name": tool_name,
            "description": "do a thing", "input_preview": "{}"}


def test_relay_reply_tool_always_allowed(relay):
    relay["set_inflight"](None)  # even with NO in-flight turn
    params = _perm_params(channel_mod.OUR_REPLY_TOOL)
    anyio.run(channel_mod._handle_permission, None, _cfg(), params)
    assert relay["sent"] == [("vaxrc", "allow")]
    assert relay["routed"] == []  # never routed to Teams


def test_relay_admin_turn_auto_allows(relay):
    # admin stamp on a message ADDRESSED to this identity ("x" is _cfg()'s identity)
    relay["set_inflight"]({"id": "m1", "recipient_session": "x",
                           "metadata": {"triggering_admin": True}})
    anyio.run(channel_mod._handle_permission, None, _cfg(), _perm_params("Bash"))
    assert relay["sent"] == [("vaxrc", "allow")]
    assert relay["routed"] == []  # admin => no Teams round-trip


def test_relay_admin_stamp_on_broadcast_does_not_auto_allow(relay):
    # Defense-in-depth: triggering_admin on a NON-addressed (broadcast/forged) message must NOT
    # auto-allow — it falls through to routing instead.
    relay["set_inflight"]({"id": "m1b", "recipient_session": None,
                           "metadata": {"triggering_admin": True}})
    anyio.run(channel_mod._handle_permission, None, _cfg(), _perm_params("Bash"))
    assert relay["routed"] and relay["routed"][0]["tool_name"] == "Bash"
    assert relay["sent"] == [("vaxrc", "deny")]


def test_relay_nonadmin_consequential_routes_to_teams(relay):
    relay["set_inflight"]({"id": "m2", "metadata": {}})  # no triggering_admin
    anyio.run(channel_mod._handle_permission, None, _cfg(), _perm_params("Bash"))
    assert relay["routed"] and relay["routed"][0]["tool_name"] == "Bash"
    assert relay["sent"] == [("vaxrc", "deny")]  # applies the routed verdict


def test_relay_nonadmin_readonly_auto_passes(relay):
    relay["set_inflight"]({"id": "m3", "metadata": {}})
    anyio.run(channel_mod._handle_permission, None, _cfg(), _perm_params("Read"))
    assert relay["sent"] == [("vaxrc", "allow")]
    assert relay["routed"] == []  # read-only never bothers Teams


def test_relay_no_inflight_is_silent(relay):
    # operator's OWN local turn (no channel message in flight) => leave to the local dialog
    relay["set_inflight"](None)
    anyio.run(channel_mod._handle_permission, None, _cfg(), _perm_params("Bash"))
    assert relay["sent"] == [] and relay["routed"] == []


# -------------------------------------------------- reply tool -> mesh reply


@pytest.fixture
def reply_rig(monkeypatch):
    calls = []

    async def fake_mesh_reply(cfg, message_id, response):
        calls.append((message_id, response))
        return True

    monkeypatch.setattr(channel_mod, "_mesh_reply", fake_mesh_reply)

    def _set_inflight(mid):
        channel_mod._RT = channel_mod._Runtime(cfg=_cfg(enabled=True))
        channel_mod._RT.inflight = {"id": mid} if mid else None

    yield {"calls": calls, "set_inflight": _set_inflight}
    channel_mod._RT = None


def test_reply_tool_relays_and_signals(reply_rig):
    reply_rig["set_inflight"]("m1")
    out = anyio.run(channel_mod._call_tool, "reply", {"message_id": "m1", "response": "done"})
    assert reply_rig["calls"] == [("m1", "done")]
    assert channel_mod._RT.reply_event.is_set()  # unblocks the inbox loop for the next claim
    assert "delivered" in out[0].text


def test_reply_tool_mismatched_id_relays_but_no_signal(reply_rig):
    reply_rig["set_inflight"]("m2")  # in-flight is a DIFFERENT message
    anyio.run(channel_mod._call_tool, "reply", {"message_id": "m1", "response": "late"})
    assert reply_rig["calls"] == [("m1", "late")]  # still relays to the controller
    assert not channel_mod._RT.reply_event.is_set()  # but does not advance the loop


def test_reply_tool_accepts_text_alias(reply_rig):
    reply_rig["set_inflight"]("m1")
    anyio.run(channel_mod._call_tool, "reply", {"message_id": "m1", "text": "via alias"})
    assert reply_rig["calls"] == [("m1", "via alias")]


def test_call_tool_unknown_raises(reply_rig):
    reply_rig["set_inflight"]("m1")
    with pytest.raises(ValueError):
        anyio.run(channel_mod._call_tool, "nope", {})


# -------------------------------------------------- send_teams tool -> hub outbox


def test_send_teams_tool_listed():
    tools = anyio.run(channel_mod._list_tools)
    names = {t.name for t in tools}
    assert "send_teams" in names and "reply" in names


def test_instructions_carry_generic_teams_formatting():
    """Teams conventions must live in the server instructions (the only context guaranteed present
    in every channel session on every peer/repo) and stay repo-agnostic — no evolv-ultra chat
    names / repos / JIRA host hardcoded, so they never mislead a session in a different repo."""
    instr = channel_mod.INSTRUCTIONS
    assert "## Teams formatting (MANDATORY)" in instr
    assert "pipe-tables" in instr and "row" in instr.lower()
    assert "git rev-parse" in instr  # full 40-char SHA rule
    assert "No emojis" in instr
    # Repo-specific detail is deferred to the working repo's Teams skill, NOT hardcoded here.
    for leak in ("ULTRADEV TEAM CHAT", "ULTRA TEAM CHAT", "evolv-ultra-be", "evolving-ai"):
        assert leak not in instr, f"repo-specific {leak!r} leaked into generic server instructions"


def test_tool_descriptions_point_to_teams_formatting():
    tools = {t.name: t for t in anyio.run(channel_mod._list_tools)}
    for name in ("send_teams", "reply"):
        desc = tools[name].description or ""
        assert "Teams conventions in the server instructions" in desc
        assert "no emojis" in desc.lower()


def test_send_teams_is_auto_pass():
    cfg = _cfg()
    assert channel_mod._is_auto_pass(channel_mod.OUR_SEND_TEAMS_TOOL, cfg) is True


def test_relay_send_teams_tool_always_allowed(relay):
    relay["set_inflight"](None)  # even with NO in-flight turn (the hub re-gates it)
    params = _perm_params(channel_mod.OUR_SEND_TEAMS_TOOL)
    anyio.run(channel_mod._handle_permission, None, _cfg(), params)
    assert relay["sent"] == [("vaxrc", "allow")]
    assert relay["routed"] == []


@pytest.fixture
def send_teams_rig(monkeypatch):
    calls = []  # list of (text, target, metadata)

    async def fake_mesh_send_teams(cfg, text, target, metadata):
        calls.append((text, target, metadata))
        # Simulate the hub: it posts for admin-triggered OR operator-direct requests.
        if metadata.get("triggering_admin") or metadata.get("operator_direct"):
            return {"ok": True, "detail": f"delivered to '{target or 'origin'}'"}
        return {"ok": False, "detail": "refused: neither admin-triggered nor operator-direct"}

    monkeypatch.setattr(channel_mod, "_mesh_send_teams", fake_mesh_send_teams)

    def _set_inflight(inflight):
        channel_mod._RT = channel_mod._Runtime(cfg=_cfg(enabled=True, identity="x"))
        channel_mod._RT.inflight = inflight

    yield {"calls": calls, "set_inflight": _set_inflight}
    channel_mod._RT = None


def test_send_teams_admin_turn_stamps_and_posts(send_teams_rig):
    send_teams_rig["set_inflight"](
        {"id": "m1", "recipient_session": "x",
         "metadata": {"triggering_admin": True, "conversation_id": "conv-1"}}
    )
    out = anyio.run(
        channel_mod._call_tool, "send_teams", {"text": "build green", "target": "Eng"}
    )
    text, target, meta = send_teams_rig["calls"][0]
    assert text == "build green" and target == "Eng"
    assert meta["triggering_admin"] is True
    assert meta["conversation_id"] == "conv-1"
    assert "posted to Teams" in out[0].text


def test_send_teams_default_target_is_origin(send_teams_rig):
    send_teams_rig["set_inflight"](
        {"id": "m1", "recipient_session": "x",
         "metadata": {"triggering_admin": True, "conversation_id": "conv-1"}}
    )
    anyio.run(channel_mod._call_tool, "send_teams", {"text": "done"})  # no target
    _, target, _ = send_teams_rig["calls"][0]
    assert target is None


def test_send_teams_unaddressed_admin_not_stamped(send_teams_rig):
    # triggering_admin set but message NOT addressed to this identity => stamp is dropped.
    send_teams_rig["set_inflight"](
        {"id": "m1", "recipient_session": None, "metadata": {"triggering_admin": True}}
    )
    out = anyio.run(channel_mod._call_tool, "send_teams", {"text": "x", "target": "Eng"})
    _, _, meta = send_teams_rig["calls"][0]
    assert meta["triggering_admin"] is False
    assert "NOT posted" in out[0].text


def test_send_teams_operator_direct_posts(send_teams_rig):
    # No task in flight => the operator is driving directly => trusted (operator_direct).
    send_teams_rig["set_inflight"](None)
    out = anyio.run(channel_mod._call_tool, "send_teams", {"text": "x", "target": "Eng"})
    _, target, meta = send_teams_rig["calls"][0]
    assert meta.get("operator_direct") is True
    assert "triggering_admin" not in meta
    assert target == "Eng"
    assert "posted to Teams" in out[0].text


def test_send_teams_via_fleet_channel_omits_origin_when_inflight(send_teams_rig):
    # ECA-113: a routine progress post fired while a task is still in-flight must NOT inherit
    # that task's own origin (which would echo it back to whoever dispatched the task) — it
    # omits conversation_id/origin_message_id entirely so the hub's fleet.py falls through to
    # deliver_via_thread instead.
    send_teams_rig["set_inflight"](
        {"id": "m1", "recipient_session": "x",
         "metadata": {"triggering_admin": True, "conversation_id": "conv-1"}}
    )
    out = anyio.run(
        channel_mod._call_tool, "send_teams",
        {"text": "wave 2 kicked off", "via_fleet_channel": True},
    )
    text, target, meta = send_teams_rig["calls"][0]
    assert text == "wave 2 kicked off" and target is None
    assert meta["triggering_admin"] is True
    assert "conversation_id" not in meta
    assert "origin_message_id" not in meta
    assert "Fleet channel thread" in out[0].text


def test_send_teams_via_fleet_channel_ignored_when_target_given(send_teams_rig):
    # An explicit target always wins — via_fleet_channel must not suppress the normal origin
    # stamping when the caller also names a destination.
    send_teams_rig["set_inflight"](
        {"id": "m1", "recipient_session": "x",
         "metadata": {"triggering_admin": True, "conversation_id": "conv-1"}}
    )
    anyio.run(
        channel_mod._call_tool, "send_teams",
        {"text": "x", "target": "Eng", "via_fleet_channel": True},
    )
    _, target, meta = send_teams_rig["calls"][0]
    assert target == "Eng"
    assert meta["conversation_id"] == "conv-1"
    assert meta["origin_message_id"] == "m1"


def test_send_teams_via_fleet_channel_unaddressed_admin_not_stamped(send_teams_rig):
    # The triggering_admin gate still applies unchanged under via_fleet_channel — an unaddressed
    # pushed task still fails safe (refused), not silently promoted.
    send_teams_rig["set_inflight"](
        {"id": "m1", "recipient_session": None, "metadata": {"triggering_admin": True}}
    )
    out = anyio.run(
        channel_mod._call_tool, "send_teams", {"text": "x", "via_fleet_channel": True}
    )
    _, _, meta = send_teams_rig["calls"][0]
    assert meta["triggering_admin"] is False
    assert "NOT posted" in out[0].text


def test_send_teams_via_fleet_channel_noop_when_no_inflight(send_teams_rig):
    # Operator-direct sends already omit conversation_id — via_fleet_channel is a harmless no-op,
    # including in the reported destination text (it must not claim a Fleet-channel delivery it
    # had no part in).
    send_teams_rig["set_inflight"](None)
    out = anyio.run(
        channel_mod._call_tool, "send_teams", {"text": "x", "via_fleet_channel": True}
    )
    _, target, meta = send_teams_rig["calls"][0]
    assert target is None
    assert meta.get("operator_direct") is True
    assert "conversation_id" not in meta
    assert "Fleet channel" not in out[0].text


def test_send_teams_requires_text(send_teams_rig):
    send_teams_rig["set_inflight"]({"id": "m1", "recipient_session": "x", "metadata": {}})
    out = anyio.run(channel_mod._call_tool, "send_teams", {"text": "   "})
    assert send_teams_rig["calls"] == []  # never reaches the mesh
    assert "required" in out[0].text


# -------------------------------------------------- session-to-session relay (ADR-0015)


def test_session_tools_listed():
    names = {t.name for t in anyio.run(channel_mod._list_tools)}
    assert "list_sessions" in names and "send_to_session" in names


def test_session_tools_are_auto_pass():
    cfg = _cfg(auto_pass_tools=frozenset())  # even with NO read-only allowlist
    assert channel_mod._is_auto_pass(channel_mod.OUR_LIST_SESSIONS_TOOL, cfg) is True
    assert channel_mod._is_auto_pass(channel_mod.OUR_SEND_TO_SESSION_TOOL, cfg) is True


def test_relay_session_tools_always_allowed(relay):
    # Delivery/control paths: allowed regardless of in-flight turn, never routed to Teams.
    relay["set_inflight"](None)
    anyio.run(
        channel_mod._handle_permission, None, _cfg(),
        _perm_params(channel_mod.OUR_SEND_TO_SESSION_TOOL),
    )
    assert relay["sent"] == [("vaxrc", "allow")]
    assert relay["routed"] == []


def test_instructions_carry_session_messaging():
    instr = channel_mod.INSTRUCTIONS
    assert "## Talking to the operator's other sessions" in instr
    assert "list_sessions()" in instr
    assert "send_to_session(" in instr
    assert "wait_for_reply" in instr


@pytest.fixture
def session_rig(monkeypatch):
    """Record _mesh_session_op calls and return a configured result."""
    calls = []
    result_box = {"ok": True, "result": {}}

    async def fake_op(cfg, op, payload, timeout):
        calls.append({"op": op, "payload": payload, "timeout": timeout})
        return result_box["result_value"] if "result_value" in result_box else result_box

    monkeypatch.setattr(channel_mod, "_mesh_session_op", fake_op)
    channel_mod._RT = channel_mod._Runtime(cfg=_cfg(enabled=True))
    yield {"calls": calls, "set_result": lambda v: result_box.__setitem__("result_value", v)}
    channel_mod._RT = None


def test_list_sessions_formats_result(session_rig):
    session_rig["set_result"](
        {"ok": True, "result": {"sessions": [{"identity": "mbpm2.backend", "repo": "backend"}]}}
    )
    out = anyio.run(channel_mod._call_tool, "list_sessions", {})
    assert session_rig["calls"][0]["op"] == "list"
    assert "mbpm2.backend" in out[0].text


def test_list_sessions_empty(session_rig):
    session_rig["set_result"]({"ok": True, "result": {"sessions": []}})
    out = anyio.run(channel_mod._call_tool, "list_sessions", {})
    assert "no other live sessions" in out[0].text


def test_send_to_session_requires_target_and_text(session_rig):
    out = anyio.run(channel_mod._call_tool, "send_to_session", {"text": "hi"})
    assert "`target` is required" in out[0].text
    out2 = anyio.run(channel_mod._call_tool, "send_to_session", {"target": "mbpm2.backend"})
    assert "`text` is required" in out2[0].text
    assert session_rig["calls"] == []  # never reached the mesh


def test_send_to_session_notify_passes_payload(session_rig):
    session_rig["set_result"]({"ok": True, "result": {"delivered": True, "ready": False}})
    out = anyio.run(
        channel_mod._call_tool, "send_to_session",
        {"target": "mbpm2.backend", "text": "rebase pls"},
    )
    call = session_rig["calls"][0]
    assert call["op"] == "send"
    assert call["payload"]["target"] == "mbpm2.backend"
    assert call["payload"]["text"] == "rebase pls"
    assert call["payload"]["wait_for_reply"] is False
    assert call["timeout"] == 60.0  # short budget for notify
    assert "delivered" in out[0].text


def test_send_to_session_wait_for_reply_await_outlasts_hub_wait(session_rig):
    session_rig["set_result"]({"ok": True, "result": {"ready": True, "reply": "on dev"}})
    anyio.run(
        channel_mod._call_tool, "send_to_session",
        {"target": "mbpm2.backend", "text": "branch?", "wait_for_reply": True, "wait_seconds": 90},
    )
    call = session_rig["calls"][0]
    assert call["payload"]["wait_for_reply"] is True
    assert call["payload"]["wait_seconds"] == 90  # clamped W is sent to the hub (no drift)
    # the local await must OUTLAST the hub's W-second wait, else a slow target reports false failure
    assert call["timeout"] == 90.0 + channel_mod._RELAY_AWAIT_MARGIN


def test_send_to_session_wait_budget_capped(session_rig):
    session_rig["set_result"]({"ok": True, "result": {}})
    anyio.run(
        channel_mod._call_tool, "send_to_session",
        {"target": "x.y", "text": "t", "wait_for_reply": True, "wait_seconds": 9999},
    )
    call = session_rig["calls"][0]
    assert call["payload"]["wait_seconds"] == channel_mod._RELAY_WAIT_CAP  # clamped to 240
    # await = cap + margin (240 + 30 = 270), still under the mesh 300s await cap; and the cap
    # equals the hub's MESH_WAIT_CAP_S so the hub actually honors the full W we send
    assert call["timeout"] == channel_mod._RELAY_WAIT_CAP + channel_mod._RELAY_AWAIT_MARGIN


def test_send_to_session_wait_seconds_zero_uses_default(session_rig):
    session_rig["set_result"]({"ok": True, "result": {}})
    anyio.run(
        channel_mod._call_tool, "send_to_session",
        {"target": "x.y", "text": "t", "wait_for_reply": True, "wait_seconds": 0},
    )
    call = session_rig["calls"][0]
    # 0 means "use the default" (not a 0s no-wait), and the SAME W goes to hub + await budget
    assert call["payload"]["wait_seconds"] == channel_mod._RELAY_SEND_DEFAULT_WAIT
    assert call["timeout"] == channel_mod._RELAY_SEND_DEFAULT_WAIT + channel_mod._RELAY_AWAIT_MARGIN


def test_check_session_message_await_outlasts_hub_poll(session_rig):
    session_rig["set_result"]({"ok": True, "result": {"ready": False}})
    anyio.run(channel_mod._call_tool, "check_session_message", {"message_id": "abc"})
    call = session_rig["calls"][0]
    assert call["op"] == "check"
    assert call["payload"]["wait_seconds"] == channel_mod._RELAY_CHECK_DEFAULT_WAIT
    assert (
        call["timeout"]
        == channel_mod._RELAY_CHECK_DEFAULT_WAIT + channel_mod._RELAY_AWAIT_MARGIN
    )


def test_check_session_message_listed_and_auto_pass():
    names = {t.name for t in anyio.run(channel_mod._list_tools)}
    assert "check_session_message" in names
    cfg = _cfg(auto_pass_tools=frozenset())
    assert channel_mod._is_auto_pass(channel_mod.OUR_CHECK_SESSION_MESSAGE_TOOL, cfg) is True


def test_check_session_message_requires_id(session_rig):
    out = anyio.run(channel_mod._call_tool, "check_session_message", {})
    assert "`message_id` is required" in out[0].text
    assert session_rig["calls"] == []


def test_check_session_message_polls(session_rig):
    session_rig["set_result"]({"ok": True, "result": {"ready": True, "reply": "on dev"}})
    out = anyio.run(channel_mod._call_tool, "check_session_message", {"message_id": "abc"})
    call = session_rig["calls"][0]
    assert call["op"] == "check" and call["payload"]["message_id"] == "abc"
    assert "on dev" in out[0].text


# -------------------------------------------- inbox loop: expects_reply=false FYIs (ECA-58)
#
# A hub-stamped fire-and-forget message (metadata.expects_reply=false — notify sends,
# broadcasts, late-reply push-backs) must be pushed WITHOUT holding the one-in-flight
# reply-await slot (an unanswered FYI would wedge the mailbox for reply_timeout — 30 min
# default) and must be auto-finalized via mesh reply() so it doesn't sit 'delivered' until
# the 7-day TTL. Messages without the stamp keep the claim -> push -> await-reply behavior.

import asyncio  # noqa: E402


class _Res:
    def __init__(self, data):
        self.data = data


class _ScriptedClient:
    """Async-context fastmcp Client stand-in: feeds scripted wait_for_instruction messages,
    records every call, raises CancelledError when the script is exhausted (ends the loop)."""

    def __init__(self, messages):
        self._script = list(messages)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "wait_for_instruction":
            if not self._script:
                raise asyncio.CancelledError
            return _Res({"success": True, "message": self._script.pop(0)})
        if name == "reply":
            return _Res({"success": True})
        raise AssertionError(f"unexpected tool call: {name}")


class _RecordingStream:
    def __init__(self):
        self.sent = []

    async def send(self, m):
        self.sent.append(m)


def _run_inbox(monkeypatch, cfg, messages):
    client = _ScriptedClient(messages)
    monkeypatch.setattr(channel_mod, "_make_client", lambda cfg, timeout=None: client)
    stream = _RecordingStream()
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    channel_mod._RT.initialized.set()
    # ECA-71: the inbox loop won't claim until announce is confirmed; simulate the normal
    # operating state (presence loop got a successful announce) so these tests exercise claiming.
    channel_mod._RT.announce_confirmed.set()

    async def main():
        # A regression that re-introduces the reply-await for FYIs hangs here and surfaces
        # as TimeoutError instead of the expected clean CancelledError end-of-script.
        await asyncio.wait_for(channel_mod._inbox_loop(cfg, stream), timeout=5.0)

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(main())
    finally:
        rt = channel_mod._RT
        channel_mod._RT = None
    return client, stream, rt


def _pushed(stream):
    out = []
    for m in stream.sent:
        root = m.message.root
        if root.method == channel_mod.CHANNEL_METHOD:
            out.append(root.params)
    return out


def test_inbox_fyi_skips_reply_await_and_auto_acks(monkeypatch):
    cfg = _cfg(enabled=True, identity="peer.repo.a", reply_timeout=60.0)
    fyi = {
        "id": "m-fyi", "sender": "evolv-coder-agent", "prompt": "[Session message] heads-up",
        "recipient_session": "peer.repo.a",
        "metadata": {"from_session": "peer.repo.b", "expects_reply": False},
    }
    client, stream, rt = _run_inbox(monkeypatch, cfg, [fyi])

    pushed = _pushed(stream)
    assert len(pushed) == 1 and pushed[0]["meta"]["message_id"] == "m-fyi"
    # Auto-finalized on the mesh so it doesn't linger status=delivered.
    replies = [a for n, a in client.calls if n == "reply"]
    assert replies == [
        {"message_id": "m-fyi", "response": "(auto-ack: FYI delivered to the live session)"}
    ]
    # Moved straight on to the next claim (reply_timeout=60 would have hung the harness).
    waits = [a for n, a in client.calls if n == "wait_for_instruction"]
    assert len(waits) == 2
    assert rt.inflight is None


@pytest.mark.parametrize(
    "metadata",
    [None, {"from_session": "peer.repo.b"}, {"from_session": "peer.repo.b", "expects_reply": True}],
)
def test_inbox_unstamped_message_still_awaits_reply(monkeypatch, metadata):
    cfg = _cfg(enabled=True, identity="peer.repo.a", reply_timeout=0.05)
    msg = {
        "id": "m-task", "sender": "evolv-coder-agent", "prompt": "do work",
        "recipient_session": "peer.repo.a", "metadata": metadata,
    }
    client, stream, rt = _run_inbox(monkeypatch, cfg, [msg])

    assert [p["meta"]["message_id"] for p in _pushed(stream)] == ["m-task"]
    # ECA-71 Layer C: with the fast liveness signal OFF (default), a reply_timeout is AMBIGUOUS
    # (a live-but-slow turn looks identical to a dead one), so the sidecar must NOT bounce — mesh
    # reply() finalizes the message and would clobber a real late reply. It leaves the message
    # un-finalized (no reply call) and claims next; a late real reply still lands (pre-ECA-71).
    assert [n for n, _ in client.calls if n == "reply"] == []
    # It DID hold the in-flight slot (awaited reply_timeout at 0.05s), then claimed again.
    assert len([a for n, a in client.calls if n == "wait_for_instruction"]) == 2
    assert rt.inflight is None


# ---------------------------------------------- ECA-71 Layer B: never claim under a contested id
#
# The inbox loop gates every claim on announce_confirmed. A fork whose announce is refused
# (IDENTITY_LIVE_ELSEWHERE) never sets it, so it never claims — closing MISROUTE + the
# fork-without-flag black hole at their shared source.


def test_inbox_does_not_claim_until_announce_confirmed(monkeypatch):
    cfg = _cfg(enabled=True, identity="peer.repo.a")
    msg = {"id": "m1", "sender": "brain", "prompt": "do", "recipient_session": "peer.repo.a"}
    client = _ScriptedClient([msg])
    monkeypatch.setattr(channel_mod, "_make_client", lambda cfg, timeout=None: client)
    stream = _RecordingStream()
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    channel_mod._RT.initialized.set()
    # announce_confirmed deliberately LEFT UNSET (simulates a refused fork / not-yet-confirmed).

    async def main():
        await asyncio.wait_for(channel_mod._inbox_loop(cfg, stream), timeout=0.3)

    try:
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(main())  # blocks on announce_confirmed.wait(), never reaches the client
    finally:
        channel_mod._RT = None

    # It never claimed — no wait_for_instruction, nothing pushed.
    assert [n for n, _ in client.calls if n == "wait_for_instruction"] == []
    assert _pushed(stream) == []


# ---------------------------------------------- ECA-71 Layer C: degrade after K non-consumptions
#
# After degrade_after consecutive non-consumptions the sidecar disarms its claim loop (and the
# presence loop re-announces channel=false/degraded so the brain reroutes to notify+pull).


def test_inbox_degrades_and_stops_claiming_after_k_nonconsumptions(monkeypatch, tmp_path):
    # DEAD (bounce + degrade) requires POSITIVE death evidence — the fast liveness signal with a
    # status file whose updated_at never advances after the push.
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"updated_at": 100.0}))  # never advances -> dead consumer
    cfg = _cfg(
        enabled=True, identity="peer.repo.a", reply_timeout=0.05, heartbeat=0.01, degrade_after=1,
        liveness_check_enabled=True, liveness_window_s=0.02, status_file=str(sf),
    )
    msg = {"id": "m1", "sender": "brain", "prompt": "do", "recipient_session": "peer.repo.a"}
    # Three messages queued, but a degrade_after=1 must stop claiming after the FIRST bounce.
    client = _ScriptedClient([msg, msg, msg])
    monkeypatch.setattr(channel_mod, "_make_client", lambda cfg, timeout=None: client)
    stream = _RecordingStream()
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    channel_mod._RT.initialized.set()
    channel_mod._RT.announce_confirmed.set()

    async def main():
        # Once degraded, the loop sleeps on heartbeat forever -> the timeout is the clean end.
        await asyncio.wait_for(channel_mod._inbox_loop(cfg, stream), timeout=0.5)

    try:
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(main())
    finally:
        rt = channel_mod._RT
        channel_mod._RT = None

    assert rt.degraded is True
    # Bounced the one message it claimed (never black-holed)...
    assert {"message_id": "m1", "response": channel_mod._NON_CONSUMPTION_BOUNCE} in [
        a for n, a in client.calls if n == "reply"
    ]
    # ...and then stopped claiming: only ONE claim happened despite three queued messages.
    assert len([1 for n, _ in client.calls if n == "wait_for_instruction"]) == 1


# -------------------------------- FMC-9 Bug 2: remote-context grace window
#
# _handle_send_teams used to treat `rt.inflight is None` as sufficient proof the local operator
# is directly driving the session (operator_direct trust). Two windows make that false: an
# unanswered FYI push (deliberately pushed WITHOUT holding the in-flight slot) that the agent may
# still be acting on, and the moment right after an ambiguous/unknown consumption verdict clears
# `inflight` even though the original turn may still genuinely be executing. These tests cover
# both the wiring (_inbox_loop marking/clearing the grace window) and the trust decision itself
# (_handle_send_teams consulting it).


def test_inbox_fyi_marks_remote_context_active(monkeypatch):
    cfg = _cfg(enabled=True, identity="peer.repo.a", reply_timeout=60.0)
    fyi = {
        "id": "m-fyi",
        "sender": "evolv-coder-agent",
        "prompt": "[Session message] heads-up",
        "recipient_session": "peer.repo.a",
        "metadata": {"from_session": "peer.repo.b", "expects_reply": False},
    }
    _client, _stream, rt = _run_inbox(monkeypatch, cfg, [fyi])
    assert rt.remote_turn_started_ts is not None
    assert channel_mod._remote_context_active(rt) is True


def test_inbox_unknown_verdict_marks_remote_context_active(monkeypatch):
    # Fast liveness signal OFF (default) + reply_timeout elapses -> _UNKNOWN (ambiguous, the
    # original turn may still be executing) -> the grace window must open, not stay closed.
    cfg = _cfg(enabled=True, identity="peer.repo.a", reply_timeout=0.05)
    msg = {
        "id": "m-task",
        "sender": "evolv-coder-agent",
        "prompt": "do work",
        "recipient_session": "peer.repo.a",
        "metadata": None,
    }
    _client, _stream, rt = _run_inbox(monkeypatch, cfg, [msg])
    assert rt.remote_turn_started_ts is not None
    assert channel_mod._remote_context_active(rt) is True


def test_inbox_dead_verdict_clears_remote_context(monkeypatch, tmp_path):
    # A _DEAD verdict (positive non-consumption evidence) means the turn is confirmed NOT
    # running -- unlike _UNKNOWN, this must NOT open the grace window.
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"updated_at": 100.0}))  # never advances -> dead consumer
    cfg = _cfg(
        enabled=True,
        identity="peer.repo.a",
        reply_timeout=0.05,
        heartbeat=0.01,
        liveness_check_enabled=True,
        liveness_window_s=0.02,
        status_file=str(sf),
    )
    msg = {"id": "m1", "sender": "brain", "prompt": "do", "recipient_session": "peer.repo.a"}
    _client, _stream, rt = _run_inbox(monkeypatch, cfg, [msg])
    assert rt.remote_turn_started_ts is None
    assert channel_mod._remote_context_active(rt) is False


def test_handle_send_teams_denies_trust_during_remote_context(send_teams_rig):
    # No task in flight, BUT a remote-originated turn may still be executing (grace window open)
    # -> neither trusted origin applies; the hub must fail safe and refuse (pre-fix: this got
    # operator_direct=True purely because rt.inflight was None).
    send_teams_rig["set_inflight"](None)
    channel_mod._mark_remote_context(channel_mod._RT)
    out = anyio.run(channel_mod._call_tool, "send_teams", {"text": "x", "target": "Eng"})
    _, _, meta = send_teams_rig["calls"][0]
    assert "operator_direct" not in meta
    assert "triggering_admin" not in meta
    assert "NOT posted" in out[0].text


def test_handle_send_teams_recovers_operator_direct_after_grace_window(send_teams_rig):
    # Once the grace window has genuinely elapsed, a no-in-flight send_teams call is trusted
    # again -- this is a bounded safety margin, not a permanent lockout. Backdate the mark
    # directly (real time arithmetic) rather than monkeypatching time.monotonic globally, which
    # would also break asyncio's own internals mid-run.
    send_teams_rig["set_inflight"](None)
    channel_mod._RT.remote_turn_started_ts = (
        channel_mod.time.monotonic() - channel_mod._REMOTE_CONTEXT_GRACE_S - 1.0
    )
    out = anyio.run(channel_mod._call_tool, "send_teams", {"text": "x", "target": "Eng"})
    _, _, meta = send_teams_rig["calls"][0]
    assert meta.get("operator_direct") is True
    assert "posted to Teams" in out[0].text


def test_handle_send_teams_status_file_activity_extends_grace_window(send_teams_rig, tmp_path):
    # A blind timer alone would let trust return to operator_direct once the grace period
    # elapses since the mark, even if the agent is still actively working on the remote-
    # originated turn (ordinary agentic latency, not an edge case). The status file's
    # updated_at (bumped by the real CC hooks on UserPromptSubmit/Stop) is a stronger,
    # activity-based signal that must extend the window instead of letting it lapse.
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"updated_at": channel_mod.time.time()}))  # recent activity
    cfg = _cfg(enabled=True, status_file=str(sf))
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    # Marked long enough ago that the plain monotonic timer alone would already have expired.
    channel_mod._RT.remote_turn_started_ts = (
        channel_mod.time.monotonic() - channel_mod._REMOTE_CONTEXT_GRACE_S - 1.0
    )
    out = anyio.run(channel_mod._call_tool, "send_teams", {"text": "x", "target": "Eng"})
    _, _, meta = send_teams_rig["calls"][0]
    assert "operator_direct" not in meta
    assert "NOT posted" in out[0].text


def test_handle_send_teams_stale_status_file_does_not_extend_grace_window(send_teams_rig, tmp_path):
    # A status file whose activity is ALSO stale must not artificially hold the window open --
    # only genuinely recent hook activity extends it.
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"updated_at": 100.0}))  # ancient, unrelated to "now"
    cfg = _cfg(enabled=True, status_file=str(sf))
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    channel_mod._RT.remote_turn_started_ts = (
        channel_mod.time.monotonic() - channel_mod._REMOTE_CONTEXT_GRACE_S - 1.0
    )
    out = anyio.run(channel_mod._call_tool, "send_teams", {"text": "x", "target": "Eng"})
    _, _, meta = send_teams_rig["calls"][0]
    assert meta.get("operator_direct") is True
    assert "posted to Teams" in out[0].text


# -------------------------------- ECA-71 Layer C: fast liveness signal (_await_consumption)


def test_await_consumption_consumed_when_replied():
    cfg = _cfg(enabled=True, reply_timeout=5.0)
    rt = channel_mod._Runtime(cfg=cfg)

    async def run():
        rt.reply_event.set()
        return await channel_mod._await_consumption(
            cfg, rt, baseline_ts=None, baseline_activity_ts=None
        )

    assert asyncio.run(run()) == channel_mod._CONSUMED


def test_await_consumption_dead_when_fast_signal_sees_no_life(tmp_path):
    # Fast signal ON + a non-advancing status file -> _DEAD fast (never waits reply_timeout).
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"updated_at": 100.0}))
    cfg = _cfg(
        enabled=True,
        liveness_check_enabled=True,
        liveness_window_s=0.05,
        reply_timeout=50.0,  # if the fast path were broken this would hang past the 5s wait_for
        status_file=str(sf),
    )
    rt = channel_mod._Runtime(cfg=cfg)

    async def run():
        return await asyncio.wait_for(
            channel_mod._await_consumption(
                cfg, rt, baseline_ts=100.0, baseline_activity_ts=None
            ),
            timeout=5.0,
        )

    assert asyncio.run(run()) == channel_mod._DEAD


def test_await_consumption_unknown_never_bounces_slow_turn():
    # Regression guard (reviewer finding #1): fast signal OFF, a turn that runs past reply_timeout
    # must NOT be treated as dead — a plain timeout is ambiguous, so bouncing would finalize the
    # message and discard the agent's real (late) answer. It must return _UNKNOWN (no bounce).
    cfg = _cfg(enabled=True, liveness_check_enabled=False, reply_timeout=0.03)
    rt = channel_mod._Runtime(cfg=cfg)

    async def run():
        return await channel_mod._await_consumption(
            cfg, rt, baseline_ts=None, baseline_activity_ts=None
        )

    assert asyncio.run(run()) == channel_mod._UNKNOWN


def test_await_consumption_fast_signal_inert_without_status_file():
    # Fast signal ON but NO status file -> cannot detect life -> falls back to the ambiguous
    # (never-bounce) path, returning _UNKNOWN on timeout, NOT _DEAD (guards against the
    # "no status file => every message looks dead => stuck degraded forever" trap).
    cfg = _cfg(
        enabled=True, liveness_check_enabled=True, liveness_window_s=0.05,
        reply_timeout=0.03, status_file=None,
    )
    rt = channel_mod._Runtime(cfg=cfg)

    async def run():
        return await channel_mod._await_consumption(
            cfg, rt, baseline_ts=None, baseline_activity_ts=None
        )

    assert asyncio.run(run()) == channel_mod._UNKNOWN


# ----------------------------------- ECA-83 finding #1: permission-activity liveness companion


def test_await_consumption_activity_advance_saves_queued_behind_long_turn(tmp_path):
    # A push queued behind an already-running long turn: the status file only bumped once at
    # that turn's OWN start (never again mid-turn) so status_advanced is False here — but the
    # turn's own tool calls stamp rt.last_activity_ts, which alone must be enough to avoid a
    # false _DEAD bounce of the queued push (it falls through to the ambiguous/never-bounce
    # path instead, same as a status-file advance would).
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"updated_at": 100.0}))  # frozen for the whole window
    cfg = _cfg(
        enabled=True,
        liveness_check_enabled=True,
        liveness_window_s=0.05,
        reply_timeout=0.1,
        status_file=str(sf),
    )
    rt = channel_mod._Runtime(cfg=cfg)
    rt.last_activity_ts = 200.0  # a tool-call permission round trip landed after baseline

    async def run():
        return await asyncio.wait_for(
            channel_mod._await_consumption(
                cfg, rt, baseline_ts=100.0, baseline_activity_ts=100.0
            ),
            timeout=5.0,
        )

    assert asyncio.run(run()) == channel_mod._UNKNOWN  # alive-but-slow, NOT dead -> no bounce


def test_await_consumption_dead_when_neither_status_nor_activity_advance(tmp_path):
    # Sanity converse of the above: with NEITHER signal advancing, the fast path still bounces
    # (unchanged _DEAD behavior) — the activity signal only ever ADDS life evidence, never
    # removes the existing status-file one.
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"updated_at": 100.0}))
    cfg = _cfg(
        enabled=True, liveness_check_enabled=True, liveness_window_s=0.05, reply_timeout=50.0,
        status_file=str(sf),
    )
    rt = channel_mod._Runtime(cfg=cfg)
    rt.last_activity_ts = 100.0  # no advance past baseline_activity_ts

    async def run():
        return await asyncio.wait_for(
            channel_mod._await_consumption(
                cfg, rt, baseline_ts=100.0, baseline_activity_ts=100.0
            ),
            timeout=5.0,
        )

    assert asyncio.run(run()) == channel_mod._DEAD


# --------------------------- ECA-83 finding #4: hookless-host diagnostic on first false-bounce


def test_await_consumption_dead_logs_hookless_warning_once(tmp_path, monkeypatch):
    # status_file is CONFIGURED but has never been written (no SessionStart ever ran there,
    # e.g. launched outside start-session.sh) — the first _DEAD verdict logs a distinguishing
    # WARNING naming the likely cause, and only once (a standing misconfiguration, not a
    # per-bounce event).
    sf = tmp_path / "status.json"  # deliberately never created
    cfg = _cfg(
        enabled=True, liveness_check_enabled=True, liveness_window_s=0.02, reply_timeout=0.02,
        status_file=str(sf),
    )
    rt = channel_mod._Runtime(cfg=cfg)
    logs = []
    monkeypatch.setattr(channel_mod, "_log", lambda msg: logs.append(msg))

    async def run():
        v1 = await channel_mod._await_consumption(
            cfg, rt, baseline_ts=None, baseline_activity_ts=None
        )
        v2 = await channel_mod._await_consumption(
            cfg, rt, baseline_ts=None, baseline_activity_ts=None
        )
        return v1, v2

    v1, v2 = asyncio.run(run())
    assert v1 == channel_mod._DEAD and v2 == channel_mod._DEAD
    assert rt.hookless_warning_logged is True
    warnings = [m for m in logs if "ECA-83 finding #4" in m]
    assert len(warnings) == 1  # logged once across both bounces, not per-bounce


def test_await_consumption_dead_no_hookless_warning_when_status_file_exists(tmp_path, monkeypatch):
    # Converse: a status file that DOES exist and has reported a value (just non-advancing) is a
    # genuinely dead/parked consumer, not a hookless-host misconfiguration — no warning.
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"updated_at": 100.0}))
    cfg = _cfg(
        enabled=True, liveness_check_enabled=True, liveness_window_s=0.02, reply_timeout=0.02,
        status_file=str(sf),
    )
    rt = channel_mod._Runtime(cfg=cfg)
    logs = []
    monkeypatch.setattr(channel_mod, "_log", lambda msg: logs.append(msg))

    assert asyncio.run(
        channel_mod._await_consumption(cfg, rt, baseline_ts=100.0, baseline_activity_ts=None)
    ) == channel_mod._DEAD
    assert rt.hookless_warning_logged is False
    assert not [m for m in logs if "ECA-83 finding #4" in m]


# ------------------------------------- ECA-83 finding #3: activity-driven re-arm (_maybe_rearm)


def test_maybe_rearm_noop_when_not_degraded():
    cfg = _cfg(enabled=True)
    rt = channel_mod._Runtime(cfg=cfg)
    channel_mod._maybe_rearm(cfg, rt)  # must not raise / must not touch anything
    assert rt.degraded is False


def test_maybe_rearm_via_status_advance(tmp_path):
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"updated_at": 200.0}))
    cfg = _cfg(enabled=True, status_file=str(sf))
    rt = channel_mod._Runtime(cfg=cfg)
    rt.degraded = True
    rt.degraded_status_ts = 100.0
    rt.consecutive_nonconsumption = 3

    channel_mod._maybe_rearm(cfg, rt)

    assert rt.degraded is False
    assert rt.consecutive_nonconsumption == 0
    assert rt.degraded_status_ts is None


def test_maybe_rearm_via_activity_when_status_never_advances(tmp_path):
    # ECA-83 finding #3: on a hookless host the status file is frozen forever post-degrade
    # (no SessionStart/UserPromptSubmit/Stop hook ever bumps it) — the old code could NEVER
    # re-arm here. Permission-request activity alone must still re-arm it.
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"updated_at": 100.0}))  # frozen; never advances
    cfg = _cfg(enabled=True, status_file=str(sf))
    rt = channel_mod._Runtime(cfg=cfg)
    rt.degraded = True
    rt.degraded_status_ts = 100.0
    rt.degraded_activity_ts = 100.0
    rt.consecutive_nonconsumption = 3
    rt.last_activity_ts = 150.0  # a tool call happened after degrade

    channel_mod._maybe_rearm(cfg, rt)

    assert rt.degraded is False
    assert rt.consecutive_nonconsumption == 0
    assert rt.degraded_activity_ts is None


def test_maybe_rearm_stays_degraded_when_neither_signal_advances(tmp_path):
    sf = tmp_path / "status.json"
    sf.write_text(json.dumps({"updated_at": 100.0}))
    cfg = _cfg(enabled=True, status_file=str(sf))
    rt = channel_mod._Runtime(cfg=cfg)
    rt.degraded = True
    rt.degraded_status_ts = 100.0
    rt.degraded_activity_ts = 100.0
    rt.consecutive_nonconsumption = 3
    rt.last_activity_ts = 100.0  # no advance

    channel_mod._maybe_rearm(cfg, rt)

    assert rt.degraded is True
    assert rt.consecutive_nonconsumption == 3


# ---------------------------- ECA-83: _handle_permission stamps activity for ANY tool call


def test_handle_permission_stamps_activity_for_local_turn(relay):
    # The operator's OWN local turn (no channel message in flight) must still count as activity
    # — this is the exact signal finding #1's fix depends on.
    relay["set_inflight"](None)
    assert channel_mod._RT.last_activity_ts is None
    anyio.run(channel_mod._handle_permission, None, _cfg(), _perm_params("Bash"))
    assert channel_mod._RT.last_activity_ts is not None


def test_handle_permission_stamps_activity_for_our_tools_shortcut(relay):
    relay["set_inflight"](None)
    assert channel_mod._RT.last_activity_ts is None
    params = _perm_params(channel_mod.OUR_REPLY_TOOL)
    anyio.run(channel_mod._handle_permission, None, _cfg(), params)
    assert channel_mod._RT.last_activity_ts is not None


# ------------------------------------------------- ECA-61: presence-loop reconnect after a dead
# connection (a mesh-server restart). The bug: announce() failures were swallowed inside the
# INNER loop forever, so the outer reconnect handler (_reconnect_sleep, which rebuilds the
# client) was unreachable. These tests exercise _presence_loop directly (not through a watcher
# wrapper — there isn't one in this file; the loop is launched standalone by _serve).


class _FlakyAnnounceClient:
    """Raises on every announce() call — simulates a dead connection post-mesh-restart."""

    def __init__(self, fail_exc):
        self._fail_exc = fail_exc
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, name, args):
        assert name == "announce"
        self.calls += 1
        raise self._fail_exc


def test_presence_loop_announce_failure_escapes_to_reconnect(monkeypatch):
    cfg = _cfg(enabled=True, heartbeat=0.001)
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)

    clients_built = []

    def fake_make_client(cfg, timeout=None):
        c = _FlakyAnnounceClient(ConnectionError("dead mesh"))
        clients_built.append(c)
        return c

    monkeypatch.setattr(channel_mod, "_make_client", fake_make_client)

    reconnects = []

    async def fake_reconnect_sleep(what, exc, backoff):
        reconnects.append((what, str(exc)))
        if len(reconnects) >= 2:
            raise asyncio.CancelledError
        return backoff

    monkeypatch.setattr(channel_mod, "_reconnect_sleep", fake_reconnect_sleep)

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(channel_mod._presence_loop(cfg), timeout=5.0))
    finally:
        channel_mod._RT = None

    # THE FIX: announce()'s exception escaped the inner loop and reached _reconnect_sleep — the
    # pre-fix swallow-and-continue shape meant this list would stay empty and the test would hang
    # until the 5s asyncio.wait_for timeout (a TimeoutError, not the expected CancelledError).
    assert len(reconnects) == 2
    assert all("dead mesh" in msg for _, msg in reconnects)
    assert reconnects[0][0] == "presence"
    # A fresh client per reconnect attempt — proof the dead one was actually dropped, not reused.
    assert len(clients_built) == 2


def test_presence_loop_identity_live_elsewhere_does_not_reconnect(monkeypatch):
    # A WELL-FORMED rejection (a working connection, just a refused identity) must NOT be treated
    # like a dead connection — this is the one response shape the fix deliberately keeps handled
    # in place, without reconnecting.
    cfg = _cfg(enabled=True, heartbeat=0.001)
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)

    class _RefusingClient:
        def __init__(self):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call_tool(self, name, args):
            assert name == "announce"
            self.calls += 1
            if self.calls >= 3:
                raise asyncio.CancelledError  # end the test after a few heartbeats
            return _Res({"success": False, "error": {"code": "IDENTITY_LIVE_ELSEWHERE"}})

    client = _RefusingClient()
    monkeypatch.setattr(channel_mod, "_make_client", lambda cfg, timeout=None: client)

    reconnect_calls = []

    async def fake_reconnect_sleep(what, exc, backoff):
        reconnect_calls.append(what)
        return backoff

    monkeypatch.setattr(channel_mod, "_reconnect_sleep", fake_reconnect_sleep)

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(channel_mod._presence_loop(cfg), timeout=5.0))
    finally:
        channel_mod._RT = None

    assert reconnect_calls == []  # never reconnected — the refusal was handled in place
    assert client.calls == 3


def test_presence_loop_connection_failure_clears_announce_confirmed(monkeypatch):
    """ECA-82 (finding #4, the split-connection takeover edge): a connection failure must clear
    announce_confirmed too, not just an explicit IDENTITY_LIVE_ELSEWHERE refusal — otherwise the
    inbox loop (on its own, separately-connected client) would keep claiming on a stale grant
    while a competitor wins the identity guard, unaware the presence loop's confirmation lapsed."""
    cfg = _cfg(enabled=True, heartbeat=0.001)
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    channel_mod._RT.announce_confirmed.set()  # simulate: confirmed right before the connection drop

    def fake_make_client(cfg, timeout=None):
        return _FlakyAnnounceClient(ConnectionError("dead mesh"))

    monkeypatch.setattr(channel_mod, "_make_client", fake_make_client)

    async def fake_reconnect_sleep(what, exc, backoff):
        # End the test right after the first failure; the flag must already be cleared by then.
        raise asyncio.CancelledError

    monkeypatch.setattr(channel_mod, "_reconnect_sleep", fake_reconnect_sleep)

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(channel_mod._presence_loop(cfg), timeout=5.0))
        assert not channel_mod._RT.announce_confirmed.is_set()
    finally:
        channel_mod._RT = None


# ---------------------------------------------------------- ECA-82: graceful forget on shutdown


class _ForgetClient:
    """Records forget() calls; stands in for the fresh client _make_client returns."""

    def __init__(self, raise_exc=None):
        self._raise_exc = raise_exc
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self._raise_exc is not None:
            raise self._raise_exc
        return _Res({"success": True, "identity": args.get("identity"), "deleted": True})


def test_graceful_forget_calls_tool_when_confirmed(monkeypatch):
    cfg = _cfg(enabled=True, announce_token="tok-A")
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    channel_mod._RT.announce_confirmed.set()

    client = _ForgetClient()
    monkeypatch.setattr(channel_mod, "_make_client", lambda cfg, timeout=None: client)

    try:
        asyncio.run(channel_mod._graceful_forget(cfg))
    finally:
        channel_mod._RT = None

    assert client.calls == [("forget", {"identity": cfg.identity, "announce_token": "tok-A"})]


def test_graceful_forget_skips_when_never_confirmed(monkeypatch):
    """A refused fork (never confirmed) must never call forget — it never owned the row, so
    calling would be pointless at best and risky at worst if the token check ever loosened."""
    cfg = _cfg(enabled=True, announce_token="tok-A")
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    # announce_confirmed deliberately left unset.

    client = _ForgetClient()
    monkeypatch.setattr(channel_mod, "_make_client", lambda cfg, timeout=None: client)

    try:
        asyncio.run(channel_mod._graceful_forget(cfg))
    finally:
        channel_mod._RT = None

    assert client.calls == []


def test_graceful_forget_skips_without_token(monkeypatch):
    cfg = _cfg(enabled=True, announce_token="")
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    channel_mod._RT.announce_confirmed.set()

    client = _ForgetClient()
    monkeypatch.setattr(channel_mod, "_make_client", lambda cfg, timeout=None: client)

    try:
        asyncio.run(channel_mod._graceful_forget(cfg))
    finally:
        channel_mod._RT = None

    assert client.calls == []


def test_graceful_forget_swallows_errors(monkeypatch):
    """Best-effort: a broken/unreachable server must never propagate out of shutdown."""
    cfg = _cfg(enabled=True, announce_token="tok-A")
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    channel_mod._RT.announce_confirmed.set()

    client = _ForgetClient(raise_exc=ConnectionError("mesh unreachable"))
    monkeypatch.setattr(channel_mod, "_make_client", lambda cfg, timeout=None: client)

    try:
        asyncio.run(channel_mod._graceful_forget(cfg))  # must not raise
    finally:
        channel_mod._RT = None

    assert len(client.calls) == 1


# ==================================================================================
# FMC-13: channel.py's two-part arming gate + race-prone claimed-message state
# ==================================================================================
#
# AC#1: the arming decision (both presence's channel:true advertisement and the inbox loop's
# claiming) must incorporate this session's own MCP handshake signal (notifications/initialized,
# observed by the stdio tee) rather than only cfg.enabled, and a timeout waiting for that signal
# must leave the sidecar disarmed rather than falling through to arm anyway.


class _RecordingAnnounceClient:
    """Records every announce() call's args; ends the test after `stop_after` calls."""

    def __init__(self, stop_after):
        self.calls = []
        self._stop_after = stop_after

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, name, args):
        assert name == "announce"
        self.calls.append(args)
        if len(self.calls) >= self._stop_after:
            raise asyncio.CancelledError
        return _Res({"success": True})


def test_presence_does_not_advertise_channel_before_initialized(monkeypatch):
    # Pre-fix: presence advertised channel:true purely from cfg.enabled, with no regard for
    # whether this session's own MCP handshake had ever actually been observed -- a
    # wired-but-never-actually-connected launch still told the brain it was push-capable.
    cfg = _cfg(enabled=True, heartbeat=0.001)
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    # Deliberately NOT setting rt.initialized.
    client = _RecordingAnnounceClient(stop_after=2)
    monkeypatch.setattr(channel_mod, "_make_client", lambda cfg, timeout=None: client)

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(channel_mod._presence_loop(cfg), timeout=5.0))
    finally:
        channel_mod._RT = None

    assert len(client.calls) == 2
    assert all(args["metadata"]["channel"] is False for args in client.calls)


def test_presence_advertises_channel_once_initialized(monkeypatch):
    cfg = _cfg(enabled=True, heartbeat=0.001)
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    channel_mod._RT.initialized.set()
    client = _RecordingAnnounceClient(stop_after=1)
    monkeypatch.setattr(channel_mod, "_make_client", lambda cfg, timeout=None: client)

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(channel_mod._presence_loop(cfg), timeout=5.0))
    finally:
        channel_mod._RT = None

    assert client.calls[0]["metadata"]["channel"] is True


def test_inbox_stays_disarmed_when_never_initialized(monkeypatch):
    # Pre-fix: a single 30s timeout on the initialized-wait fell through and started claiming
    # anyway. Post-fix it must never proceed without the signal, no matter how many times the
    # internal wait re-times-out. Shrink the log-interval constant so the bounded test window
    # spans SEVERAL such timeouts -- a test bounded well under the real 30s would pass on both
    # the pre-fix and post-fix code (neither reaches its first timeout that fast), so it
    # wouldn't actually exercise the fix.
    monkeypatch.setattr(channel_mod, "_INITIALIZED_WAIT_LOG_INTERVAL_S", 0.02)
    cfg = _cfg(enabled=True, identity="peer.repo.a")
    client_builds = []

    def fake_make_client(cfg, timeout=None):
        client_builds.append(1)
        return _ScriptedClient([])

    monkeypatch.setattr(channel_mod, "_make_client", fake_make_client)
    stream = _RecordingStream()
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    # Deliberately NOT setting rt.initialized or rt.announce_confirmed.

    try:
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(asyncio.wait_for(channel_mod._inbox_loop(cfg, stream), timeout=0.3))
    finally:
        channel_mod._RT = None

    assert client_builds == []  # never got past the initialized gate to even connect


# AC#2: a reply that fails to relay to the mesh must not make the inbox loop treat the claimed
# message as consumed.


def test_reply_tool_failed_relay_does_not_signal(reply_rig, monkeypatch):
    async def failing_mesh_reply(cfg, message_id, response):
        return False

    monkeypatch.setattr(channel_mod, "_mesh_reply", failing_mesh_reply)
    reply_rig["set_inflight"]("m1")
    out = anyio.run(channel_mod._call_tool, "reply", {"message_id": "m1", "response": "done"})
    # THE FIX: pre-fix this was signaled unconditionally on an id match, regardless of `ok`.
    assert not channel_mod._RT.reply_event.is_set()
    assert "WARNING" in out[0].text


# AC#3: a genuine reply arriving after the inbox loop already bounced this message must be
# distinguishable from an ordinary invalid/unknown message_id.


def test_reply_tool_after_bounce_gets_distinguishable_warning(reply_rig, monkeypatch):
    async def failing_mesh_reply(cfg, message_id, response):
        return False  # record_reply's CAS correctly refuses the already-finalized message

    monkeypatch.setattr(channel_mod, "_mesh_reply", failing_mesh_reply)
    reply_rig["set_inflight"](None)  # already cleared by the bounce, same as real _inbox_loop
    channel_mod._RT.bounced_message_ids.append("m1")
    out = anyio.run(
        channel_mod._call_tool, "reply", {"message_id": "m1", "response": "late answer"}
    )
    assert "already finalized by a non-consumption bounce" in out[0].text


def test_reply_tool_after_bounce_distinguishes_earlier_bounce_too(reply_rig, monkeypatch):
    # Adversarial-review finding on this branch: a single-scalar bounced-id tracker only
    # remembered the MOST RECENT bounce -- degrade_after allows several consecutive bounces
    # before the session disarms, so a late reply for an EARLIER bounce could arrive after a
    # LATER bounce overwrote a scalar tracker and wrongly fall back to the generic warning.
    # Bounded history (a deque, not a scalar) fixes this.
    async def failing_mesh_reply(cfg, message_id, response):
        return False

    monkeypatch.setattr(channel_mod, "_mesh_reply", failing_mesh_reply)
    reply_rig["set_inflight"](None)
    channel_mod._RT.bounced_message_ids.append("m1")  # bounced first
    channel_mod._RT.bounced_message_ids.append("m2")  # bounced second (most recent)
    out = anyio.run(
        channel_mod._call_tool, "reply", {"message_id": "m1", "response": "late answer for m1"}
    )
    assert "already finalized by a non-consumption bounce" in out[0].text


def test_reply_tool_unknown_id_keeps_generic_warning(reply_rig, monkeypatch):
    # An ordinary unknown/invalid id (not a bounced one) keeps the pre-existing generic warning
    # -- the distinguishing message is reserved for the specific bounce case.
    async def failing_mesh_reply(cfg, message_id, response):
        return False

    monkeypatch.setattr(channel_mod, "_mesh_reply", failing_mesh_reply)
    reply_rig["set_inflight"](None)
    out = anyio.run(channel_mod._call_tool, "reply", {"message_id": "typod-id", "response": "x"})
    assert "unknown/already-finalized" in out[0].text
    assert "non-consumption bounce" not in out[0].text


# AC#4: a push failure must reliably clear the claimed-message state, and while stale state
# would otherwise persist it must never be applied to an unrelated permission request.


class _RaisingPushStream:
    """A write_stream whose send() always raises (simulates a broken stdio pipe on push)."""

    def __init__(self, fail_exc):
        self._fail_exc = fail_exc

    async def send(self, m):
        raise self._fail_exc


def test_inbox_push_failure_clears_inflight_and_does_not_leak_into_permission_relay(monkeypatch):
    # Pre-fix: _push sat OUTSIDE the try/finally that clears `inflight`, so a push failure left
    # the claimed-message slot set indefinitely. While leaked, _handle_permission would evaluate
    # a later, UNRELATED permission request against the stale message's triggering_admin --
    # this message is deliberately admin-triggered + addressed, the exact shape that would have
    # been auto-allowed pre-fix.
    cfg = _cfg(enabled=True, identity="peer.repo.a")
    msg = {
        "id": "m1",
        "sender": "brain",
        "prompt": "do",
        "recipient_session": "peer.repo.a",
        "metadata": {"triggering_admin": True},
    }
    client = _ScriptedClient([msg])
    monkeypatch.setattr(channel_mod, "_make_client", lambda cfg, timeout=None: client)
    stream = _RaisingPushStream(ConnectionError("broken pipe"))
    channel_mod._RT = channel_mod._Runtime(cfg=cfg)
    channel_mod._RT.initialized.set()
    channel_mod._RT.announce_confirmed.set()

    reconnects = []

    async def fake_reconnect_sleep(what, exc, backoff):
        reconnects.append((what, str(exc)))
        raise asyncio.CancelledError  # end the test right after the push failure is handled

    monkeypatch.setattr(channel_mod, "_reconnect_sleep", fake_reconnect_sleep)

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(channel_mod._inbox_loop(cfg, stream), timeout=5.0))
    finally:
        rt = channel_mod._RT
        channel_mod._RT = None

    assert len(reconnects) == 1 and "broken pipe" in reconnects[0][1]
    # THE FIX: cleared despite it being the PUSH (not _await_consumption) that failed.
    assert rt.inflight is None

    channel_mod._RT = rt
    routed = []
    sent = []

    async def fake_route(cfg, inflight, tool_name, description, preview):
        routed.append(inflight)
        return "deny"

    async def fake_send(write_stream, request_id, behavior):
        sent.append(behavior)

    monkeypatch.setattr(channel_mod, "_route_approval", fake_route)
    monkeypatch.setattr(channel_mod, "_send_permission", fake_send)
    try:
        anyio.run(
            channel_mod._handle_permission,
            None,
            cfg,
            {"request_id": "r1", "tool_name": "Bash", "description": "x", "input_preview": "{}"},
        )
        # Falls through to the local terminal dialog (no in-flight turn), not the stale message
        # -- pre-fix this auto-allowed via the stale message's triggering_admin=True + addressed.
        assert routed == []
        assert sent == []
    finally:
        channel_mod._RT = None
