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
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from worker_supervisor.config import Config, Limits
from worker_supervisor.engine import Engine
from worker_supervisor.gate import (
    QuestionBridge,
    WorkerPolicy,
    make_gate,
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
    "field,malformed,safe_default",
    [
        ("allowed_tools", "Bash", []),  # a malformed ceiling must not silently widen
        ("allowed_tools", [1, 2, 3], []),
        ("allow_env", 42, []),
        ("allow_env", "PATH", []),
        ("guard_hooks", [], {}),
        ("limits", "not-a-dict", {}),
        ("limits", ["wall_clock_s", 5], {}),
        ("mcp_servers", [1, 2, 3], {}),
        ("mcp_servers", "jira", {}),
    ],
)
async def test_spawn_coerces_a_malformed_pass_through_field(
    control, registry, repo, field, malformed, safe_default
):
    """ECA-141 AC#1/#2: a control-socket spawn with a wrong-shaped pass-through
    field must not raise, and must fall back to a safe default — not merely avoid
    an exception while persisting the bad shape for the next turn to trip over.
    Reads the RAW stored row rather than the dispatch response, since the response
    redacts `mcp_servers` to a name list unconditionally (ECA-133) — a transform
    unrelated to this coercion.

    AC#3's literal wording ("the gate returns a decision") is closed below: the
    coerced row is reloaded exactly as a turn would (`WorkerPolicy.from_json`) and
    driven through the real `can_use_tool` gate, asserting a normal permission
    decision comes back rather than an exception.
    """
    name = f"probe-{field}"
    await control._dispatch("spawn", {"name": name, "repo": str(repo), field: malformed})
    row = await registry.get_worker(name)
    stored = json.loads(row["policy"])
    assert stored[field] == safe_default

    policy = WorkerPolicy.from_json(row["policy"])
    gate = make_gate(
        worker=name,
        repo_root=repo,
        policy=policy,
        bridge=QuestionBridge(registry, control._events),
        events=control._events,
        turn_id=1,
        question_timeout_s=1.0,
    )
    decision = await gate("Bash", {"command": "echo hi"}, None)
    assert isinstance(decision, (PermissionResultAllow, PermissionResultDeny))


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


# --- ECA-139: the `attach` verb's own error path ---------------------------------
#
# The gap that let ECA-137 ship a live regression: every test above reaches the control
# surface through `_dispatch`, and `attach` is dispatched in `_handle` BEFORE the
# try/except that wraps `_dispatch` — so no existing test could observe it. These go over
# a REAL unix socket through `_handle`, which is the only way to see what a client gets.


async def _serve(control, sock_path: Path):
    """Run the real _handle over a real socket; yields nothing, cancel to stop."""
    server = await asyncio.start_unix_server(control._handle, path=str(sock_path))
    return server


async def _round_trip(sock_path: Path, verb: str, args: dict, *, limit: int = 1):
    """Send one request, read up to `limit` lines, return them decoded."""
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write((json.dumps({"verb": verb, "args": args}) + "\n").encode())
    await writer.drain()
    lines = []
    try:
        for _ in range(limit):
            line = await asyncio.wait_for(reader.readline(), timeout=3.0)
            if not line:
                break
            lines.append(line.decode())
    finally:
        writer.close()
    return lines


@pytest.fixture
def sock():
    d = Path(tempfile.mkdtemp(dir="/tmp"))  # macOS sun_path is capped at 104 bytes
    yield d / "s.sock"
    shutil.rmtree(d, ignore_errors=True)


async def test_attach_with_a_refused_name_replies_with_an_error_not_a_silent_eof(
    control, sock
):
    """ECA-139. `EventLog.follow` raises ValueError for a refused key (ECA-137), and
    `_attach` caught only three connection exceptions — so the raise escaped into
    asyncio's client_connected_cb, the client saw EOF with NO error line and exited 0,
    and the daemon logged a traceback on every attempt.

    Asserts the CLIENT-VISIBLE bytes, because that is what was wrong: the guard worked
    perfectly and the operator could not tell.
    """
    server = await _serve(control, sock)
    try:
        lines = await _round_trip(sock, "attach", {"name": "../../escaped"})
    finally:
        server.close()
        await server.wait_closed()

    assert lines, "attach returned a silent EOF — the ECA-139 regression"
    reply = json.loads(lines[0])
    assert reply["ok"] is False
    assert "invalid event log key" in reply["error"]


async def test_attach_error_does_not_escape_into_the_asyncio_callback(control, sock):
    """The other half, and the reason a reply alone is not enough: an unhandled exception
    in `client_connected_cb` is what put a five-frame traceback in the pm2 log on every
    attempt — which in this fleet reads as 'the supervisor is broken' and invites a
    restart that tears down in-flight lane turns.
    """
    caught: list[BaseException] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: caught.append(ctx.get("exception")))

    server = await _serve(control, sock)
    try:
        await _round_trip(sock, "attach", {"name": "../../escaped"})
        await asyncio.sleep(0.05)  # let any stray callback fire
    finally:
        server.close()
        await server.wait_closed()
        loop.set_exception_handler(previous)

    assert [e for e in caught if isinstance(e, ValueError)] == []


async def test_attach_on_a_legal_name_still_streams_records(control, sock, repo):
    """AC#3: the fix must not turn a working tail into an error. Emits AFTER the client
    attaches, because `follow` deliberately starts at end-of-file."""
    await _spawn_probe(control, repo)
    server = await _serve(control, sock)
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write((json.dumps({"verb": "attach", "args": {"name": "probe"}}) + "\n").encode())
        await writer.drain()
        await asyncio.sleep(0.1)  # let follow() reach its poll loop
        control._events.emit("probe", "eca139_probe_event", n=1)
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        writer.close()
    finally:
        server.close()
        await server.wait_closed()

    record = json.loads(line)
    assert record["event"] == "eca139_probe_event"
    assert "ok" not in record, "a record must be distinguishable from an error reply"


async def test_the_events_verb_still_returns_a_clean_json_error(control, sock):
    """Pins the contrast that made the regression diagnosable: `events` goes through
    `_dispatch` and was always fine; only `attach` bypassed the wrapper."""
    server = await _serve(control, sock)
    try:
        lines = await _round_trip(sock, "events", {"name": "../../escaped"})
    finally:
        server.close()
        await server.wait_closed()

    reply = json.loads(lines[0])
    assert reply["ok"] is False and "invalid event log key" in reply["error"]
