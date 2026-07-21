"""Tests for the permission-relay tools' size-cap validation (FMC-4) and tool-pattern
deviations from CLAUDE.md's documented contract (FMC-6)."""

import pytest

from fast_mcp_claude.services.store import Store
from fast_mcp_claude.tools.permissions import (
    pending_approvals,
    request_approval,
    wait_for_pending_approval,
)
from fast_mcp_claude.utils.validation import MAX_TOOL_INPUT_BYTES, MAX_TOOL_NAME_BYTES


@pytest.fixture
def wired_request_approval(store: Store, monkeypatch):
    """request_approval() reads the `store` name bound in the permissions tool module
    (imported once from ..server at import time) -- point it at this test's isolated
    store."""
    monkeypatch.setattr("fast_mcp_claude.tools.permissions.store", store)
    return request_approval


@pytest.fixture
def wired_pending_approvals(store: Store, monkeypatch):
    monkeypatch.setattr("fast_mcp_claude.tools.permissions.store", store)
    return pending_approvals


@pytest.fixture
def wired_wait_for_pending_approval(store: Store, monkeypatch):
    monkeypatch.setattr("fast_mcp_claude.tools.permissions.store", store)
    monkeypatch.setattr("fast_mcp_claude.tools.permissions.settings", store.settings)
    return wait_for_pending_approval


@pytest.mark.asyncio
async def test_request_approval_accepts_normal_input(wired_request_approval):
    result = await wired_request_approval(
        session_id="dev", tool_name="Bash", tool_input={"command": "ls"}
    )
    assert result["success"] is True


@pytest.mark.asyncio
async def test_request_approval_rejects_oversized_tool_input(wired_request_approval):
    """FMC-4: tool_input is json.dumps'd straight into SQLite with no prior cap."""
    oversized = {"content": "x" * (MAX_TOOL_INPUT_BYTES + 1)}
    result = await wired_request_approval(session_id="dev", tool_name="Write", tool_input=oversized)
    assert result["success"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_request_approval_rejects_oversized_tool_name(wired_request_approval):
    result = await wired_request_approval(
        session_id="dev", tool_name="x" * (MAX_TOOL_NAME_BYTES + 1), tool_input={}
    )
    assert result["success"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_pending_approvals_rejects_non_integer_limit(wired_pending_approvals):
    """FMC-6 AC#1: a non-numeric limit must surface as VALIDATION_ERROR with a field,
    not the bare `except Exception` catch-all's generic UNKNOWN_ERROR."""
    result = await wired_pending_approvals(limit="not-a-number")
    assert result["success"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert result["error"]["field"] == "limit"


@pytest.mark.asyncio
async def test_pending_approvals_accepts_normal_limit(wired_pending_approvals, store: Store):
    await store.create_approval(session_id="dev", tool_name="Bash", tool_input={"command": "ls"})
    result = await wired_pending_approvals(limit=10)
    assert result["success"] is True
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_wait_for_pending_approval_returns_existing(
    wired_wait_for_pending_approval, store: Store
):
    """FMC-6 AC#2: wait_for_pending_approval must work purely through the public
    Store API (Store.wait_for_pending_approvals), no reach-in to store._notifier."""
    await store.create_approval(session_id="dev", tool_name="Bash", tool_input={"command": "ls"})
    result = await wired_wait_for_pending_approval(timeout=1)
    assert result["success"] is True
    assert len(result["approvals"]) == 1


@pytest.mark.asyncio
async def test_wait_for_pending_approval_times_out_empty(
    wired_wait_for_pending_approval,
):
    result = await wired_wait_for_pending_approval(timeout=0.05)
    assert result["success"] is True
    assert result["approvals"] == []
