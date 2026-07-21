"""First-ever ControlServer tests: socket-refusal + argv-rejection (ECA-72 AC#5/AC#6).

Socket paths use /tmp (not pytest tmp_path) because macOS AF_UNIX sun_path is
capped at 104 bytes and pytest's tmp_path can exceed that limit.
"""

from __future__ import annotations

import asyncio
import shutil
import socket as _socket
import sys
import tempfile
from pathlib import Path

import pytest

from worker_supervisor.config import Config, Limits
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
