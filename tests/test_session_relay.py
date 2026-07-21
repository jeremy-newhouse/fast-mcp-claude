"""Tests for the session_relay store queue (ADR-0015): create -> drain -> complete -> await.

Mirrors the teams_outbox queue, but carries an opaque {op, payload} request and a JSON result.
"""

import asyncio

import pytest

from fast_mcp_claude.services.store import OUTBOX_CLAIMED, OUTBOX_DONE, Store
from fast_mcp_claude.tools.session_relay import complete_session_op, request_session_op
from fast_mcp_claude.utils.validation import MAX_METADATA_BYTES


@pytest.mark.asyncio
async def test_round_trip_send(store: Store):
    rid = await store.create_session_op(
        requester="mini2.frontend",
        op="send",
        payload={"target": "mbpm2.backend", "text": "rebase pls", "wait_for_reply": False},
    )

    pending = await store.list_pending_session_ops()
    assert [p["id"] for p in pending] == [rid]
    # list_pending_session_ops atomically claims the row (FMC-12 AC#2): it is no
    # longer bare "pending" once a drain call has observed it.
    assert pending[0]["status"] == OUTBOX_CLAIMED
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
async def test_concurrent_drain_claims_op_exactly_once(store: Store):
    """Regression (FMC-12 AC#2): two concurrent hub-side drain calls racing for the
    same pending row must not both observe it as claimable — pre-fix, list_pending_
    session_ops was a bare SELECT with no claim step, so both concurrent callers
    would see (and both act on, e.g. re-route/re-execute) the same row. Combined
    across both calls, the row must surface exactly once."""
    rid = await store.create_session_op(requester="r", op="list")

    results = await asyncio.gather(
        store.list_pending_session_ops(),
        store.list_pending_session_ops(),
    )
    claimed_ids = [p["id"] for lst in results for p in lst]
    assert claimed_ids == [rid]  # not [], and not [rid, rid]


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
async def test_cleanup_removes_stale_claimed(store: Store):
    """Regression (FMC-12): a row claimed by a drain call that then crashed before
    completing must not dangle in the claimed state forever -- cleanup's stale-expiry
    sweep must catch CLAIMED rows too, not just PENDING ones."""
    rid = await store.create_session_op(requester="r", op="list")
    await store.list_pending_session_ops()  # claims it; hub then "crashes" (never completes)
    await store.db.execute("UPDATE session_relay SET created_at=? WHERE id=?", (1000.0, rid))
    await store._cleanup_once(cutoff=2000.0)
    assert await store.get_session_op(rid) is None


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


@pytest.fixture
def wired_session_relay_tools(store: Store, monkeypatch):
    """request_session_op()/complete_session_op() read the `store` name bound in the
    session_relay tool module -- point it at this test's isolated store."""
    monkeypatch.setattr("fast_mcp_claude.tools.session_relay.store", store)
    return request_session_op, complete_session_op


@pytest.mark.asyncio
async def test_request_session_op_rejects_oversized_payload(wired_session_relay_tools):
    """FMC-4: request_session_op's payload is json.dumps'd straight into SQLite with no
    prior cap."""
    request_session_op_fn, _ = wired_session_relay_tools
    oversized = {"text": "x" * (MAX_METADATA_BYTES + 1)}
    result = await request_session_op_fn(op="send", payload=oversized)
    assert result["success"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_complete_session_op_rejects_oversized_result(wired_session_relay_tools):
    """FMC-4: complete_session_op's result is json.dumps'd straight into SQLite with no
    prior cap -- rejected before the (nonexistent) request_id is even looked up."""
    _, complete_session_op_fn = wired_session_relay_tools
    oversized = {"sessions": "x" * (MAX_METADATA_BYTES + 1)}
    result = await complete_session_op_fn(request_id="a" * 32, ok=True, result=oversized)
    assert result["success"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"
