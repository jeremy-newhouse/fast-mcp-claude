"""ControlServer tests: socket-refusal + argv-rejection (ECA-72 AC#5/AC#6), and
the response-boundary credential redaction (ECA-133).

Socket paths use /tmp (not pytest tmp_path) because macOS AF_UNIX sun_path is
capped at 104 bytes and pytest's tmp_path can exceed that limit.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import socket as _socket
import sys
import tempfile
from pathlib import Path

import pytest

from worker_supervisor.config import Config, Limits
from worker_supervisor.engine import Engine
from worker_supervisor.gate import (
    QuestionBridge,
    WorkerPolicy,
    redact_policy,
    redact_worker_row,
)
from worker_supervisor.server import ControlServer
from worker_supervisor.__main__ import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, sock_path: Path) -> Config:
    """Minimal Config for preflight tests — matches conftest.py constructor style."""
    return Config(
        home=tmp_path / "home",
        limits=Limits(),
        question_timeout_s=1,
        cycle_context_pct=80,
        max_concurrent_turns=1,
        idle_timeout_s=3600,
        mesh_url=None,
        mesh_api_key=None,
        machine="testhost",
        announce_interval_s=60,
        mcp_startup_grace_s=0.0,
        socket_override=sock_path,
    )


def _cs(cfg: Config) -> ControlServer:
    """ControlServer with stub engine/registry/events — preflight only touches cfg."""
    return ControlServer(cfg, engine=None, registry=None, events=None)  # type: ignore[arg-type]


@pytest.fixture
def sock_dir():
    """Short-path temp dir under /tmp — safe for macOS AF_UNIX 104-char limit."""
    d = Path(tempfile.mkdtemp(prefix="ws-test-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_preflight_refuses_live_socket(tmp_path, sock_dir):
    """A second daemon boot must raise SystemExit(1) when a listener already holds
    the socket (real incident 2026-07-07 — AC#5/ECA-72)."""
    sock_path = sock_dir / "s.sock"
    cfg = _cfg(tmp_path, sock_path)

    listener = await asyncio.start_unix_server(
        lambda r, w: None, path=str(sock_path)
    )
    try:
        with pytest.raises(SystemExit) as exc_info:
            await _cs(cfg).preflight_socket_check()
        assert exc_info.value.code == 1
    finally:
        listener.close()
        await listener.wait_closed()


async def test_preflight_allows_stale_socket_file(tmp_path, sock_dir):
    """A stale socket file (no listener) must not block boot (connection refused
    → probe returns False → preflight_socket_check returns normally)."""
    sock_path = sock_dir / "s.sock"
    cfg = _cfg(tmp_path, sock_path)

    # Bind without listen/accept → file exists but ECONNREFUSED on connect.
    raw = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    raw.bind(str(sock_path))
    raw.close()
    assert sock_path.exists()

    # Must not raise.
    await _cs(cfg).preflight_socket_check()


def test_argv_rejection(monkeypatch):
    """Passing any argument to the daemon entry point must exit(2) with a pointer
    to the `workers` CLI — operators running `worker-supervisor status` by hand
    must not accidentally boot a daemon (AC#6/ECA-72)."""
    monkeypatch.setattr(sys, "argv", ["worker-supervisor", "status"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# ECA-133: no control-surface response may carry MCP credentials
# ---------------------------------------------------------------------------

# Distinctive, obviously-fake markers. Credentials ride in at least two shapes —
# an http server's `headers` and a stdio server's `env` — which is why the fix
# collapses the whole block to server names rather than redacting by key name.
HEADER_SENTINEL = "ECA133-HEADER-SENTINEL-NOT-A-REAL-SECRET"
ENV_SENTINEL = "ECA133-ENV-SENTINEL-NOT-A-REAL-SECRET"
ARG_SENTINEL = "ECA133-ARG-SENTINEL-NOT-A-REAL-SECRET"
URL_SENTINEL = "ECA133-URL-SENTINEL-NOT-A-REAL-SECRET"
SENTINELS = (HEADER_SENTINEL, ENV_SENTINEL, ARG_SENTINEL, URL_SENTINEL)

MCP_SERVERS_WITH_SECRETS = {
    "jira": {
        "type": "http",
        "url": f"https://example.invalid/mcp?token={URL_SENTINEL}",
        "headers": {"Authorization": f"Bearer {HEADER_SENTINEL}"},
    },
    "langfuse": {
        "command": "/bin/true",
        "args": ["--api-key", ARG_SENTINEL],
        "env": {"LANGFUSE_SECRET": ENV_SENTINEL},
    },
}


@pytest.fixture
async def control(cfg, registry, events, monkeypatch):
    """A real ControlServer over a real Engine + Registry.

    The per-worker runner loop is parked so no turn can ever be claimed: these
    tests are about what the control surface SERIALIZES, and a live runner would
    otherwise pick up the turn minted below and launch a real Claude subprocess.
    """

    async def _parked(self, name: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_worker_loop", _parked)
    engine = Engine(cfg, registry, events, QuestionBridge(registry, events))
    yield ControlServer(cfg, engine, registry, events)
    await engine.stop()


def _wire(data) -> str:
    """Exactly what `_reply` puts on the socket, so assertions cover the bytes a
    caller actually sees — not a friendlier in-memory view of them."""
    return json.dumps({"ok": True, "data": data}, default=str)


async def _spawn_probe(control, repo, name: str = "probe"):
    return await control._dispatch(
        "spawn",
        {
            "name": name,
            "repo": str(repo),
            "allowed_tools": ["Read"],
            "mcp_servers": MCP_SERVERS_WITH_SECRETS,
        },
    )


async def test_spawn_response_carries_no_credential_material(control, repo):
    """AC#1: a spawn response must not echo any credential the caller passed in.

    The pre-fix daemon returned the stored policy verbatim, so a single spawn put
    every granted server's bearer/env value into the caller's transcript.
    """
    data = await _spawn_probe(control, repo)
    wire = _wire(data)
    for sentinel in SENTINELS:
        assert sentinel not in wire, f"{sentinel} leaked into the spawn response"

    policy = json.loads(data["worker"]["policy"])
    # The caller still learns WHICH servers were granted — just not their secrets.
    assert policy["mcp_servers"] == ["jira", "langfuse"]
    # Everything else about the applied policy survives; only the block changed.
    assert policy["allowed_tools"] == ["Read"]


async def test_spawn_still_stores_the_real_credentials(control, registry, repo):
    """Negative control: redaction is a RESPONSE concern only.

    The engine launches each MCP server from the stored policy, so blanking the
    stored row would break every credentialed lane — a 'fix' that passes the leak
    test by breaking the feature.
    """
    await _spawn_probe(control, repo)
    row = await registry.get_worker("probe")
    stored = json.loads(row["policy"])
    assert stored["mcp_servers"]["jira"]["headers"]["Authorization"] == (
        f"Bearer {HEADER_SENTINEL}"
    )
    assert stored["mcp_servers"]["langfuse"]["env"]["LANGFUSE_SECRET"] == ENV_SENTINEL


@pytest.mark.parametrize(
    "verb",
    ["status", "events", "history", "questions", "get"],
)
async def test_no_other_control_verb_leaks_credentials(control, registry, repo, verb):
    """AC#2: the same credentials must not resurface through any other verb.

    Every verb reachable without launching a turn subprocess is swept. Note what
    each case is worth: `status` and `events` are real forward guards (both
    project from live policy-derived data), while `history`/`get`/`questions`
    read tables with no policy column at all — those cases are cheap boundary
    documentation, not defect detectors. Reverting the spawn fix does not make
    them fail, and that is expected.
    """
    await _spawn_probe(control, repo)
    turn_id = await registry.enqueue_turn("probe", "hello")
    # The one event that carries MCP context is emitted per-turn, and the parked
    # runner means no turn ever runs — so emit it directly. Without this the
    # `events` case never sees the record that could plausibly grow a leak.
    control._engine._emit_mcp_diagnostics(
        "probe",
        turn_id,
        WorkerPolicy(mcp_servers=MCP_SERVERS_WITH_SECRETS),
        mcp_init=[{"name": "jira", "status": "connected"}],
        stderr_tail=["mcp server startup line"],
    )

    args = {
        "status": {},
        "events": {"name": "probe"},
        "history": {"name": "probe"},
        "questions": {"name": "probe"},
        "get": {"turn_id": turn_id},
    }[verb]
    wire = _wire(await control._dispatch(verb, args))
    for sentinel in SENTINELS:
        assert sentinel not in wire, f"{sentinel} leaked through the {verb!r} verb"


async def test_mcp_diagnostics_event_records_names_not_configs(control, registry, repo):
    """The per-turn MCP diagnostics event is the nearest thing to a second leak:
    it fires only for credentialed lanes and rides both `events` and `attach`.

    It records `sorted(mcp_servers.keys())` today. Pin that, so widening it to
    the granted server CONFIGS is a visible decision rather than silent drift.
    """
    await _spawn_probe(control, repo)
    control._engine._emit_mcp_diagnostics(
        "probe",
        1,
        WorkerPolicy(mcp_servers=MCP_SERVERS_WITH_SECRETS),
        mcp_init=None,
        stderr_tail=[],
    )
    records = control._events.read("probe")
    diagnostics = [r for r in records if r["event"] == "turn_mcp_diagnostics"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["granted"] == ["jira", "langfuse"]
    # Also assert it against the on-disk JSONL: `events` reads that file back,
    # and the log outlives the process.
    assert not any(
        s in control._events.path("probe").read_text() for s in SENTINELS
    )


def test_redact_policy_collapses_every_server_shape():
    """AC#3: the helper keeps the granted server NAMES and nothing else."""
    out = redact_policy(
        {"allowed_tools": ["Read"], "mcp_servers": MCP_SERVERS_WITH_SECRETS}
    )
    assert out["mcp_servers"] == ["jira", "langfuse"]
    assert out["allowed_tools"] == ["Read"]
    for sentinel in SENTINELS:
        assert sentinel not in json.dumps(out)


def test_redact_policy_is_idempotent_and_shape_safe():
    """Re-redacting is a no-op; an unrecognized mcp_servers value never passes
    through (it could be anything, including a credential)."""
    once = redact_policy({"mcp_servers": MCP_SERVERS_WITH_SECRETS})
    assert redact_policy(once) == once
    assert redact_policy({"mcp_servers": HEADER_SENTINEL})["mcp_servers"] == []
    assert redact_policy({"mcp_servers": None})["mcp_servers"] is None
    assert "mcp_servers" not in redact_policy({"model": "opus"})
    # The input is never mutated in place. Deep-copied on purpose: a shallow copy
    # would share the nested server dicts with the module-level constant, so an
    # in-place redaction would corrupt it for every test that runs afterwards —
    # and a later leak test would then pass against already-blank values.
    src = {"mcp_servers": copy.deepcopy(MCP_SERVERS_WITH_SECRETS)}
    redact_policy(src)
    assert src["mcp_servers"]["jira"]["headers"]["Authorization"].endswith(
        HEADER_SENTINEL
    )
    assert src["mcp_servers"]["langfuse"]["env"]["LANGFUSE_SECRET"] == ENV_SENTINEL


def test_redact_worker_row_handles_every_policy_shape():
    """Live rows always carry the TEXT column, so `policy` is always a `str`; the
    dict branch is defensive for future callers, not a path in use today.

    Everything that is neither a JSON object nor absent is withheld — the helper
    cannot prove such a value holds no secret, and raising instead would turn a
    spawn that actually succeeded into an error reply.
    """
    as_string = redact_worker_row(
        {"name": "w", "policy": json.dumps({"mcp_servers": MCP_SERVERS_WITH_SECRETS})}
    )
    assert json.loads(as_string["policy"])["mcp_servers"] == ["jira", "langfuse"]

    as_dict = redact_worker_row({"name": "w", "policy": {"mcp_servers": MCP_SERVERS_WITH_SECRETS}})
    assert as_dict["policy"]["mcp_servers"] == ["jira", "langfuse"]

    assert redact_worker_row({"name": "w", "policy": "{not json"})["policy"] is None
    assert redact_worker_row({"name": "w", "policy": None})["policy"] is None
    assert redact_worker_row({"name": "w"}) == {"name": "w"}

    # Valid JSON that is not an object: withheld, never raised.
    for not_an_object in ("[1, 2]", '"a string"', "123", "true", "null"):
        assert redact_worker_row({"name": "w", "policy": not_an_object})["policy"] is None
    assert redact_worker_row({"name": "w", "policy": 123})["policy"] is None
