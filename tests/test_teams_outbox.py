"""Tests for the teams_outbox store queue (ADR-0013): create -> drain -> complete -> await."""

import asyncio
import base64

import pytest

from fast_mcp_claude.services.store import OUTBOX_CLAIMED, OUTBOX_DONE, Store
from fast_mcp_claude.tools.teams_outbox import request_teams_send
from fast_mcp_claude.utils.validation import MAX_FILE_BYTES, MAX_METADATA_BYTES


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
    # list_pending_teams_sends atomically claims the row (FMC-12 AC#1): it is no
    # longer bare "pending" once a drain call has observed it.
    assert pending[0]["status"] == OUTBOX_CLAIMED
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
async def test_concurrent_drain_claims_row_exactly_once(store: Store):
    """Regression (FMC-12 AC#1): two concurrent hub-side drain calls racing for the
    same pending row must not both observe it as claimable — pre-fix, list_pending_
    teams_sends was a bare SELECT with no claim step, so both concurrent callers
    would see (and both act on, e.g. post to Teams twice for) the same row. Combined
    across both calls, the row must surface exactly once."""
    rid = await store.create_teams_send(requester="r", text="hi")

    results = await asyncio.gather(
        store.list_pending_teams_sends(),
        store.list_pending_teams_sends(),
    )
    claimed_ids = [p["id"] for lst in results for p in lst]
    assert claimed_ids == [rid]  # not [], and not [rid, rid]


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
async def test_cleanup_removes_stale_claimed(store: Store):
    """Regression (FMC-12): a row claimed by a drain call that then crashed before
    completing must not dangle in the claimed state forever -- cleanup's stale-expiry
    sweep must catch CLAIMED rows too, not just PENDING ones."""
    rid = await store.create_teams_send(requester="r", text="hi")
    await store.list_pending_teams_sends()  # claims it; hub then "crashes" (never completes)
    await store.db.execute("UPDATE teams_outbox SET created_at=? WHERE id=?", (1000.0, rid))
    await store._cleanup_once(cutoff=2000.0)
    assert await store.get_teams_send(rid) is None


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


# ── ECA-117: attachment field ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attachment_round_trips_through_store(store: Store):
    attachment = {
        "name": "report.html",
        "mime": "text/html",
        "content_b64": base64.b64encode(b"<html></html>").decode(),
    }
    rid = await store.create_teams_send(requester="r", text="hi", attachment=attachment)

    pending = await store.list_pending_teams_sends()
    assert pending[0]["attachment"] == attachment

    await store.complete_teams_send(rid, ok=True, detail="delivered")
    record = await store.get_teams_send(rid)
    assert record["attachment"] == attachment


@pytest.mark.asyncio
async def test_no_attachment_round_trips_as_none(store: Store):
    rid = await store.create_teams_send(requester="r", text="hi")
    record = await store.get_teams_send(rid)
    assert record["attachment"] is None


@pytest.mark.asyncio
async def test_request_teams_send_accepts_valid_attachment(wired_request_teams_send, store: Store):
    attachment = {
        "name": "x.txt",
        "mime": "text/plain",
        "content_b64": base64.b64encode(b"hello").decode(),
    }
    result = await wired_request_teams_send(text="hi", attachment=attachment)
    assert result["success"] is True
    record = await store.get_teams_send(result["request_id"])
    assert record["attachment"] == attachment


@pytest.mark.asyncio
async def test_request_teams_send_rejects_missing_attachment_fields(wired_request_teams_send):
    result = await wired_request_teams_send(text="hi", attachment={"name": "x.txt"})
    assert result["success"] is False


@pytest.mark.asyncio
async def test_request_teams_send_rejects_invalid_base64(wired_request_teams_send):
    result = await wired_request_teams_send(
        text="hi",
        attachment={"name": "x.txt", "mime": "text/plain", "content_b64": "not-valid-base64!!"},
    )
    assert result["success"] is False


@pytest.mark.asyncio
async def test_request_teams_send_rejects_oversized_attachment(wired_request_teams_send):
    oversized = base64.b64encode(b"x" * (MAX_FILE_BYTES + 1)).decode()
    result = await wired_request_teams_send(
        text="hi",
        attachment={"name": "x.bin", "mime": "application/octet-stream", "content_b64": oversized},
    )
    assert result["success"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"
