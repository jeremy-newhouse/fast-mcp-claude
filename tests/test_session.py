"""Tests for fast-mcp-claude-session — the live-session presence + inbox-notify sidecar.

Focus: strict-opt-in enabled gate + identity precedence (mirrors channel), presence built
from the hook-written status file, the inbox watcher (filters to THIS identity, notifies
once per new message, NEVER claims), badge writing, and the parent-pid lifetime watch.
"""

import asyncio
import json

import pytest

from fast_mcp_claude import config as config_mod
from fast_mcp_claude import session as session_mod
from fast_mcp_claude.config import Settings

_SESSION_ENV = (
    "SESSION_ENABLED",
    "SESSION_NOTIFY",
    "CRM_IDENTITY",
    "CRM_SUMMARY",
    "CRM_LOCAL_URL",
    "CRM_SESSION_STATUS_FILE",
    "CRM_SESSION_BADGE_FILE",
    "CRM_SESSION_POLL_S",
    "CRM_SESSION_HEARTBEAT_S",
    "MCP_API_KEY",
)


@pytest.fixture
def env(monkeypatch):
    for name in _SESSION_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def fake_settings(monkeypatch):
    def _install(**overrides) -> Settings:
        base = dict(
            peer_name="peer-default",
            mcp_port=5499,
            mcp_api_key=None,
            mcp_auth_enabled=False,
            session_enabled=False,
            session_notify=True,
            session_status_file="",
            session_poll_s=10,
            session_heartbeat_s=15,
        )
        base.update(overrides)
        s = Settings(**base)
        monkeypatch.setattr(config_mod, "get_settings", lambda: s)
        return s

    return _install


def _cfg(**overrides) -> session_mod.SessionConfig:
    base = dict(
        identity="mini2.demo",
        local_url="http://127.0.0.1:5499/mcp",
        api_key=None,
        status_file=None,
        badge_file=None,
        summary=None,
        poll=10.0,
        heartbeat=15.0,
        enabled=True,
        notify=True,
        parent_pid=0,
    )
    base.update(overrides)
    return session_mod.SessionConfig(**base)


# --------------------------------------------------------------- enabled gate / precedence


def test_disabled_by_default(env, fake_settings):
    fake_settings(session_enabled=False)
    assert session_mod._resolve_config([]).enabled is False


def test_env_enables_over_settings_off(env, fake_settings):
    fake_settings(session_enabled=False)
    env.setenv("SESSION_ENABLED", "true")
    assert session_mod._resolve_config([]).enabled is True


def test_cli_no_enabled_wins(env, fake_settings):
    fake_settings(session_enabled=True)
    env.setenv("SESSION_ENABLED", "true")
    assert session_mod._resolve_config(["--no-enabled"]).enabled is False


def test_identity_cli_beats_env_and_peer(env, fake_settings):
    fake_settings(peer_name="pn")
    env.setenv("CRM_IDENTITY", "env-id")
    assert session_mod._resolve_config(["--identity", "cli.id"]).identity == "cli.id"


def test_identity_defaults_to_peer_name(env, fake_settings):
    fake_settings(peer_name="pn")
    assert session_mod._resolve_config([]).identity == "pn"


def test_notify_can_be_disabled_via_cli(env, fake_settings):
    fake_settings(session_notify=True)
    assert session_mod._resolve_config(["--no-notify"]).notify is False


# ------------------------------------------------------------------- presence from status


def test_build_presence_from_status_file(tmp_path):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({
        "machine": "mini2", "repo": "evolv-coder-agent", "cwd": "/r/evolv-coder-agent",
        "branch": "main", "status": "working", "last": "build phase 4",
        "updated_at": 123.0,
    }))
    summary, meta = session_mod._build_presence(_cfg(status_file=str(sf)))
    assert "evolv-coder-agent" in summary and "working" in summary and "build phase 4" in summary
    assert meta["role"] == "live-session"
    assert meta["machine"] == "mini2" and meta["repo"] == "evolv-coder-agent"
    assert meta["status"] == "working" and meta["last"] == "build phase 4"


def test_build_presence_falls_back_to_summary_when_no_status(tmp_path):
    cfg = _cfg(status_file=str(tmp_path / "missing.json"), summary="idle blurb")
    summary, meta = session_mod._build_presence(cfg)
    assert summary == "idle blurb"
    assert meta["role"] == "live-session"


def test_build_presence_includes_name(tmp_path):
    # ADR-0016: the session name (seeded from --name / git branch) is published in presence.
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({
        "machine": "mini2", "repo": "api", "name": "planning",
        "branch": "feat-x", "status": "working", "last": "y", "updated_at": 1.0,
    }))
    summary, meta = session_mod._build_presence(_cfg(status_file=str(sf)))
    assert meta["name"] == "planning"
    assert "planning" in summary


def test_build_presence_includes_description_and_claude_session_id(tmp_path):
    # ADR-0016 Amendment 1 (ECA-23): same status-file pipeline as name.
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({
        "machine": "mini2", "repo": "api",
        "session_description": "fixing the auth bug", "claude_session_id": "abc-123",
        "branch": "feat-x", "status": "working", "updated_at": 1.0,
    }))
    _, meta = session_mod._build_presence(_cfg(status_file=str(sf)))
    assert meta["session_description"] == "fixing the auth bug"
    assert meta["claude_session_id"] == "abc-123"


# ----------------------------------------------------------------- inbox watcher (no claim)


class _FakeClient:
    def __init__(self, queued):
        self._queued = queued
        self.calls = []

    async def call_tool(self, tool, args):
        self.calls.append((tool, args))
        from types import SimpleNamespace

        return SimpleNamespace(
            data={"success": True, "messages": self._queued, "count": len(self._queued)},
            content=[],
        )


def _msg(mid, ident, sender="evolv-coder-agent", prompt="hello"):
    return {"id": mid, "recipient_session": ident, "sender": sender, "prompt": prompt,
            "status": "queued"}


async def test_check_inbox_notifies_once_per_new_and_never_claims(tmp_path, monkeypatch):
    notes = []
    monkeypatch.setattr(session_mod, "_notify_macos", lambda title, message: notes.append(message))
    badge = tmp_path / "b.badge"
    cfg = _cfg(identity="mini2.demo", badge_file=str(badge), notify=True)
    watch = session_mod._Watch()

    # First poll: one message for us, one for another identity (must be ignored).
    client = _FakeClient([_msg("m1", "mini2.demo"), _msg("x", "other.repo")])
    await session_mod._check_inbox(client, cfg, watch)
    assert len(notes) == 1 and "/fleet-inbox mini2.demo" in notes[0]
    assert badge.read_text() == "1"
    # NEVER claims: only list_messages was called, no wait_for_instruction/pop.
    assert [c[0] for c in client.calls] == ["list_messages"]
    # and it is scoped server-side to THIS identity (index-backed, can't miss under load)
    assert client.calls[0][1].get("recipient_session") == "mini2.demo"

    # Second poll, same message still queued: no duplicate notification.
    await session_mod._check_inbox(_FakeClient([_msg("m1", "mini2.demo")]), cfg, watch)
    assert len(notes) == 1

    # Third poll, inbox drained (operator pulled): badge resets to 0.
    await session_mod._check_inbox(_FakeClient([]), cfg, watch)
    assert badge.read_text() == "0"


async def test_check_inbox_notify_off_still_tracks_badge(tmp_path, monkeypatch):
    notes = []
    monkeypatch.setattr(session_mod, "_notify_macos", lambda title, message: notes.append(message))
    badge = tmp_path / "b.badge"
    cfg = _cfg(identity="mini2.demo", badge_file=str(badge), notify=False)
    client = _FakeClient([_msg("m1", "mini2.demo")])
    await session_mod._check_inbox(client, cfg, session_mod._Watch())
    assert notes == []  # notify disabled
    assert badge.read_text() == "1"


# --------------------------------------------------------------------- parent-pid lifetime


def test_parent_dead_logic(monkeypatch):
    assert session_mod._parent_dead(0) is False  # unset / unwatched
    monkeypatch.setattr(session_mod.os, "getppid", lambda: 999)
    assert session_mod._parent_dead(999) is False  # still our parent
    assert session_mod._parent_dead(12345) is True  # reparented -> session gone


# --------------------------------------------------------- ECA-61: reconnect after a dead
# connection (a mesh-server restart). The bug: announce()/list_messages() failures were
# swallowed inside the INNER loop forever, so _bridge's outer reconnect handler (which rebuilds
# the client) was unreachable.


async def test_check_inbox_failure_propagates_not_swallowed():
    # _check_inbox used to swallow call_tool failures and return silently, which meant _bridge's
    # caller never saw them either. It must now propagate so the caller's reconnect logic fires.
    class _RaisingClient:
        async def call_tool(self, tool, args):
            raise ConnectionError("dead mesh")

    with pytest.raises(ConnectionError, match="dead mesh"):
        await session_mod._check_inbox(
            _RaisingClient(), _cfg(identity="mini2.demo"), session_mod._Watch()
        )


async def test_bridge_announce_failure_escapes_to_outer_reconnect(monkeypatch):
    cfg = _cfg(identity="mini2.demo", poll=0.001, heartbeat=0.001)
    clients_built = []

    class _FlakyBridgeClient:
        def __init__(self):
            clients_built.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call_tool(self, name, args):
            from types import SimpleNamespace

            if name == "announce":
                raise ConnectionError("dead mesh")
            return SimpleNamespace(data={"success": True, "messages": []}, content=[])

    monkeypatch.setattr("fastmcp.Client", lambda *a, **k: _FlakyBridgeClient())

    # _bridge never exits on its own — bound it with a REAL wall-clock timeout (asyncio.sleep is
    # NOT faked here, so the loop's own outer backoff sleep is what the fix must actually reach
    # for this to finish inside the window). Pre-fix, the swallow means exactly ONE client is
    # ever built no matter how long this runs; this asserts on that count, not on a stall/hang —
    # the test still completes promptly either way via the timeout.
    #
    # Window sizing: reaching 2 client rebuilds only needs the FIRST backoff sleep (starts at
    # 1.0s) to complete. An independent review measured ~1.73s real wall time against an earlier
    # 1.5s window — reliable in that testing, but with limited margin. 3.5s leaves generous
    # headroom for scheduler jitter on a slow/contended CI runner without meaningfully slowing
    # the suite — exactly the kind of marginal timing assumption a fix for a reconnect/timing bug
    # shouldn't itself carry.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(session_mod._bridge(cfg), timeout=3.5)

    assert len(clients_built) >= 2
