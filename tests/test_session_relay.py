"""Tests for the session_relay store queue (ADR-0015): create -> drain -> complete -> await.

Mirrors the teams_outbox queue, but carries an opaque {op, payload} request and a JSON result.
"""

import asyncio

import pytest

from fast_mcp_claude.services.store import OUTBOX_DONE, OUTBOX_PENDING, Store


@pytest.mark.asyncio
async def test_round_trip_send(store: Store):
    rid = await store.create_session_op(
        requester="mini2.frontend",
        op="send",
        payload={"target": "mbpm2.backend", "text": "rebase pls", "wait_for_reply": False},
    )

    pending = await store.list_pending_session_ops()
    assert [p["id"] for p in pending] == [rid]
    assert pending[0]["status"] == OUTBOX_PENDING
    assert pending[0]["op"] == "send"
    assert pending[0]["requester"] == "mini2.frontend"
    assert pending[0]["payload"]["target"] == "mbpm2.backend"
    assert pending[0]["ok"] is None
    assert pending[0]["result"] is None

    # The hub completes; an awaiter unblocks with the result.
    async def awaiter():
        return await store.wait_for_session_op_result(rid, timeout=5.0)

    task = asyncio.create_task(awaiter())
    await asyncio.sleep(0.05)
    assert await store.complete_session_op(
        rid, ok=True, result={"ready": False, "delivered": True, "identity": "mbpm2.backend-1"}
    )
    record = await task
    assert record is not None
    assert record["status"] == OUTBOX_DONE
    assert record["ok"] is True
    assert record["result"]["delivered"] is True
    assert record["result"]["identity"] == "mbpm2.backend-1"

    # Drained: no longer pending.
    assert await store.list_pending_session_ops() == []


@pytest.mark.asyncio
async def test_list_op_no_payload(store: Store):
    rid = await store.create_session_op(requester="mini2.frontend", op="list")
    pending = await store.list_pending_session_ops()
    assert pending[0]["id"] == rid
    assert pending[0]["op"] == "list"
    assert pending[0]["payload"] is None


@pytest.mark.asyncio
async def test_complete_twice_is_not_completable(store: Store):
    rid = await store.create_session_op(requester="r", op="list")
    assert await store.complete_session_op(rid, ok=True, result={"sessions": []}) is True
    # second completion: already finalized
    assert await store.complete_session_op(rid, ok=False, result={"error": "x"}) is False


@pytest.mark.asyncio
async def test_complete_unknown_is_false(store: Store):
    assert await store.complete_session_op("deadbeef" * 4, ok=True) is False


@pytest.mark.asyncio
async def test_wait_times_out_while_pending(store: Store):
    rid = await store.create_session_op(requester="r", op="list")
    assert await store.wait_for_session_op_result(rid, timeout=0.05) is None


@pytest.mark.asyncio
async def test_wait_for_pending_returns_on_create(store: Store):
    """A create() must wake a blocked wait_for_pending_session_ops (notifier round-trip)."""

    async def waiter():
        return await store.wait_for_pending_session_ops(timeout=5.0)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    rid = await store.create_session_op(requester="r", op="list")
    got = await task
    assert [g["id"] for g in got] == [rid]


@pytest.mark.asyncio
async def test_cleanup_removes_stale_pending(store: Store):
    rid = await store.create_session_op(requester="r", op="list")
    await store.db.execute("UPDATE session_relay SET created_at=? WHERE id=?", (1000.0, rid))
    await store._cleanup_once(cutoff=2000.0)
    assert await store.get_session_op(rid) is None
    assert await store.list_pending_session_ops() == []


@pytest.mark.asyncio
async def test_cleanup_deletes_old_completed(store: Store):
    rid = await store.create_session_op(requester="r", op="list")
    await store.complete_session_op(rid, ok=True, result={"sessions": []})
    await store.db.execute("UPDATE session_relay SET created_at=? WHERE id=?", (1000.0, rid))
    await store._cleanup_once(cutoff=2000.0)
    assert await store.get_session_op(rid) is None


@pytest.mark.asyncio
async def test_cleanup_spares_fresh_rows(store: Store):
    rid = await store.create_session_op(requester="r", op="list")  # created_at = now
    await store._cleanup_once(cutoff=1000.0)  # cutoff far in the past
    pending = await store.list_pending_session_ops()
    assert [p["id"] for p in pending] == [rid]  # still pending, untouched
