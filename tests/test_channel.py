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
    relay["set_inflight"]({"id": "m1", "metadata": {"triggering_admin": True}})
    anyio.run(channel_mod._handle_permission, None, _cfg(), _perm_params("Bash"))
    assert relay["sent"] == [("vaxrc", "allow")]
    assert relay["routed"] == []  # admin => no Teams round-trip


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
