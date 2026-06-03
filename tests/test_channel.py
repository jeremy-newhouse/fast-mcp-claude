"""Tests for the fast-mcp-claude-channel adapter.

Focus: the strict-opt-in `enabled` gate and the identity/summary precedence in
`_resolve_config` (CLI flag > env var > Settings default), plus the key safety
behavior in `_serve` — a disabled adapter completes the MCP handshake but never
starts the inbox-polling bridge, so a wired-but-unintended channel entry can't
claim messages out from under /worker loop mode.

The live two-session push path still requires launching a real worker with
`--dangerously-load-development-channels` and is exercised manually.
"""

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
    """Stub the stdio transport + server.run, and count _bridge starts."""
    calls = {"bridge": 0}

    async def fake_bridge(write_stream, cfg):
        calls["bridge"] += 1
        await anyio.sleep_forever()

    async def fake_run(read_stream, write_stream, init_options):
        # Yield long enough that a started child task (the bridge) gets a slot,
        # then return as if stdin closed and the session ended.
        await anyio.sleep(0.05)

    monkeypatch.setattr(channel_mod, "_bridge", fake_bridge)
    monkeypatch.setattr(channel_mod, "stdio_server", lambda: _DummyStreams())
    monkeypatch.setattr(channel_mod._server, "run", fake_run)
    return calls


def test_serve_disabled_never_starts_bridge(patched_serve):
    # The core invariant: disabled => handshake only, no polling/claiming/pushing.
    anyio.run(channel_mod._serve, _cfg(enabled=False))
    assert patched_serve["bridge"] == 0


def test_serve_enabled_starts_bridge(patched_serve):
    anyio.run(channel_mod._serve, _cfg(enabled=True))
    assert patched_serve["bridge"] == 1
