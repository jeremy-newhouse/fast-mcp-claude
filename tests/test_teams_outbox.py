"""Tests for the teams_outbox store queue (ADR-0013): create -> drain -> complete -> await."""

import asyncio

import pytest

from fast_mcp_claude.services.store import OUTBOX_DONE, OUTBOX_PENDING, Store
from fast_mcp_claude.tools.teams_outbox import request_teams_send
from fast_mcp_claude.utils.validation import MAX_METADATA_BYTES


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


@pytest.mark.asyncio
async def test_cleanup_removes_stale_pending(store: Store):
    # A stale PENDING row is expired (pending -> done) and pruned in the same sweep, so it
    # never dangles in the pending set. A broken expire UPDATE would leave it pending; a broken
    # delete would leave the row behind — this asserts both: it's gone and not pending.
    rid = await store.create_teams_send(requester="r", text="hi")
    await store.db.execute("UPDATE teams_outbox SET created_at=? WHERE id=?", (1000.0, rid))
    await store._cleanup_once(cutoff=2000.0)
    assert await store.get_teams_send(rid) is None
    assert await store.list_pending_teams_sends() == []


@pytest.mark.asyncio
async def test_cleanup_deletes_old_completed(store: Store):
    rid = await store.create_teams_send(requester="r", text="hi")
    await store.complete_teams_send(rid, ok=True, detail="done")
    await store.db.execute("UPDATE teams_outbox SET created_at=? WHERE id=?", (1000.0, rid))
    await store._cleanup_once(cutoff=2000.0)
    assert await store.get_teams_send(rid) is None  # pruned


@pytest.mark.asyncio
async def test_cleanup_spares_fresh_rows(store: Store):
    rid = await store.create_teams_send(requester="r", text="hi")  # created_at = now
    await store._cleanup_once(cutoff=1000.0)  # cutoff far in the past
    pending = await store.list_pending_teams_sends()
    assert [p["id"] for p in pending] == [rid]  # still pending, untouched


@pytest.fixture
def wired_request_teams_send(store: Store, monkeypatch):
    """request_teams_send() reads the `store` name bound in the teams_outbox tool
    module -- point it at this test's isolated store."""
    monkeypatch.setattr("fast_mcp_claude.tools.teams_outbox.store", store)
    return request_teams_send


@pytest.mark.asyncio
async def test_request_teams_send_rejects_oversized_metadata(wired_request_teams_send):
    """FMC-4: request_teams_send's metadata is json.dumps'd straight into SQLite with
    no prior cap."""
    oversized = {"blob": "x" * (MAX_METADATA_BYTES + 1)}
    result = await wired_request_teams_send(text="hi", metadata=oversized)
    assert result["success"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"
