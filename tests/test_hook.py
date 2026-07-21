"""Tests for hook.py's authenticated permission-relay path (FMC-14).

Regression coverage for the bug where _relay() constructed its fastmcp Client
with a `headers` kwarg the installed fastmcp Client constructor does not
accept at all, making the entire authenticated relay path unreachable dead
code for any deployment with MCP_API_KEY configured.
"""

import asyncio
import contextlib
import socket

import pytest
from fastmcp import Client, FastMCP

from fast_mcp_claude import hook as H
from fast_mcp_claude.auth import ApiKeyVerifier
from fast_mcp_claude.services.store import Store
from fast_mcp_claude.tools import permissions as permissions_mod

API_KEY = "hook-relay-test-key"


def _bind_ephemeral_socket() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock, sock.getsockname()[1]


@pytest.fixture
async def relay_server(monkeypatch, settings_factory):
    """Serve the REAL request_approval/await_decision/pending_approvals/approve_tool
    tools -- rebound to an isolated Store, not the process-wide singleton -- over a
    real HTTP port guarded by the real ApiKeyVerifier. This exercises hook.py's
    authenticated relay path against genuine installed fastmcp Client/server/auth
    code, not stand-ins."""
    settings = settings_factory(mcp_auth_enabled=True, mcp_api_key=API_KEY)
    test_store = Store(settings)
    await test_store.initialize()
    monkeypatch.setattr(permissions_mod, "store", test_store)
    monkeypatch.setattr(permissions_mod, "settings", settings)

    test_mcp = FastMCP(name="hook-relay-test", auth=ApiKeyVerifier(api_key=API_KEY))
    for fn in (
        permissions_mod.request_approval,
        permissions_mod.await_decision,
        permissions_mod.pending_approvals,
        permissions_mod.approve_tool,
    ):
        test_mcp.tool(fn)

    sock, port = _bind_ephemeral_socket()
    url = f"http://127.0.0.1:{port}/mcp"
    server_task = asyncio.create_task(
        test_mcp.run_http_async(
            host="127.0.0.1", sockets=[sock], show_banner=False, log_level="error"
        )
    )
    try:
        last_exc: Exception | None = None
        for _ in range(100):
            try:
                async with Client(url, auth=API_KEY) as c:
                    await c.call_tool("pending_approvals", {})
                break
            except Exception as e:
                last_exc = e
                await asyncio.sleep(0.02)
        else:
            raise RuntimeError(f"test relay server never became ready: {last_exc}")

        yield url
    finally:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        # Hard-cancelling uvicorn skips its normal socket cleanup.
        sock.close()
        await test_store.close()


async def _await_pending_approval_id(controller: Client) -> str:
    for _ in range(200):
        res = await controller.call_tool("pending_approvals", {"limit": 10})
        data = H._result_data(res)
        approvals = data.get("approvals") or []
        if approvals:
            return approvals[0]["id"]
        await asyncio.sleep(0.02)
    raise AssertionError("hook never created a pending approval")


async def test_relay_authenticated_allow_round_trip(relay_server):
    """AC#1/#2/#3: hook._relay() constructs a real fastmcp Client against an
    authenticated server and completes a full request_approval -> approve_tool ->
    await_decision round trip, emitting the controller's decision instead of
    falling back to ask. Pre-fix, Client(url, headers=...) raised a TypeError
    before request_approval was ever reached."""
    url = relay_server

    hook_task = asyncio.create_task(
        H._relay(url, API_KEY, "worker-session", "Bash", {"command": "echo ok"}, 10.0)
    )
    async with Client(url, auth=API_KEY) as controller:
        approval_id = await _await_pending_approval_id(controller)
        decide_res = await controller.call_tool(
            "approve_tool",
            {"approval_id": approval_id, "decision": "allow", "reason": "approved in test"},
        )
        assert H._result_data(decide_res)["success"] is True

    decision, reason = await hook_task
    assert decision == "allow"
    assert reason == "approved in test"


async def test_relay_authenticated_deny_round_trip(relay_server):
    url = relay_server

    hook_task = asyncio.create_task(
        H._relay(url, API_KEY, "worker-session", "Bash", {"command": "rm -rf /"}, 10.0)
    )
    async with Client(url, auth=API_KEY) as controller:
        approval_id = await _await_pending_approval_id(controller)
        await controller.call_tool(
            "approve_tool",
            {"approval_id": approval_id, "decision": "deny", "reason": "nope"},
        )

    decision, reason = await hook_task
    assert decision == "deny"
    assert reason == "nope"


async def test_relay_rejects_wrong_api_key(relay_server):
    """Guards the auth boundary itself: a wrong bearer must fail the relay, not
    silently succeed unauthenticated."""
    url = relay_server
    with pytest.raises(Exception):
        await H._relay(url, "totally-wrong-key", "s", "Bash", {}, 2.0)
