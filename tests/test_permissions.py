"""Tests for the permission-relay tools' size-cap validation (FMC-4)."""

import pytest

from fast_mcp_claude.services.store import Store
from fast_mcp_claude.tools.permissions import request_approval
from fast_mcp_claude.utils.validation import MAX_TOOL_INPUT_BYTES, MAX_TOOL_NAME_BYTES


@pytest.fixture
def wired_request_approval(store: Store, monkeypatch):
    """request_approval() reads the `store` name bound in the permissions tool module
    (imported once from ..server at import time) -- point it at this test's isolated
    store."""
    monkeypatch.setattr("fast_mcp_claude.tools.permissions.store", store)
    return request_approval


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
