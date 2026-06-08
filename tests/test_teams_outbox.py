"""Tests for the teams_outbox store queue (ADR-0013): create -> drain -> complete -> await."""

import asyncio

import pytest

from fast_mcp_claude.services.store import OUTBOX_DONE, OUTBOX_PENDING, Store


@pytest.mark.asyncio
async def test_round_trip(store: Store):
    rid = await store.create_teams_send(
        requester="mini2.repo",
        text="deploy green",
        target="Engineering",
        metadata={"triggering_admin": True, "conversation_id": "conv-1"},
    )

    pending = await store.list_pending_teams_sends()
    assert [p["id"] for p in pending] == [rid]
    assert pending[0]["status"] == OUTBOX_PENDING
    assert pending[0]["text"] == "deploy green"
    assert pending[0]["target"] == "Engineering"
    assert pending[0]["metadata"]["triggering_admin"] is True
    assert pending[0]["ok"] is None

    # The hub completes; an awaiter unblocks with the result.
    async def awaiter():
        return await store.wait_for_teams_send_result(rid, timeout=5.0)

    task = asyncio.create_task(awaiter())
    await asyncio.sleep(0.05)
    assert await store.complete_teams_send(rid, ok=True, detail="delivered to 'Engineering'")
    record = await task
    assert record is not None
    assert record["status"] == OUTBOX_DONE
    assert record["ok"] is True
    assert record["detail"] == "delivered to 'Engineering'"

    # Drained: no longer pending.
    assert await store.list_pending_teams_sends() == []


@pytest.mark.asyncio
async def test_complete_twice_is_not_completable(store: Store):
    rid = await store.create_teams_send(requester="r", text="hi")
    assert await store.complete_teams_send(rid, ok=True) is True
    # second completion: already finalized
    assert await store.complete_teams_send(rid, ok=False, detail="x") is False


@pytest.mark.asyncio
async def test_complete_unknown_is_false(store: Store):
    assert await store.complete_teams_send("deadbeef" * 4, ok=True) is False


@pytest.mark.asyncio
async def test_wait_times_out_while_pending(store: Store):
    rid = await store.create_teams_send(requester="r", text="hi")
    # still pending -> None on timeout
    assert await store.wait_for_teams_send_result(rid, timeout=0.05) is None


@pytest.mark.asyncio
async def test_default_target_none(store: Store):
    rid = await store.create_teams_send(requester="r", text="hi")  # no target
    pending = await store.list_pending_teams_sends()
    assert pending[0]["id"] == rid
    assert pending[0]["target"] is None
