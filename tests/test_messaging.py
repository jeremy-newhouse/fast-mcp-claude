"""Tests for the messaging tools' size-cap validation (FMC-4)."""

import pytest

from fast_mcp_claude.services.store import Store
from fast_mcp_claude.tools.messaging import send_prompt
from fast_mcp_claude.utils.validation import MAX_METADATA_BYTES


@pytest.fixture
def wired_send_prompt(store: Store, monkeypatch):
    """send_prompt() reads the `store` name bound in the messaging tool module
    (imported once from ..server at import time) -- point it at this test's
    isolated store."""
    monkeypatch.setattr("fast_mcp_claude.tools.messaging.store", store)
    return send_prompt


@pytest.mark.asyncio
async def test_send_prompt_accepts_small_metadata(wired_send_prompt):
    result = await wired_send_prompt(prompt="hi", metadata={"foo": "bar"})
    assert result["success"] is True


@pytest.mark.asyncio
async def test_send_prompt_rejects_oversized_metadata(wired_send_prompt):
    """FMC-4: send_prompt's metadata is json.dumps'd straight into SQLite -- an
    oversized value must be rejected before it ever reaches the store."""
    oversized = {"blob": "x" * (MAX_METADATA_BYTES + 1)}
    result = await wired_send_prompt(prompt="hi", metadata=oversized)
    assert result["success"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"
