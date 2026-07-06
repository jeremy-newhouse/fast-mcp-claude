"""Registry discipline: claim/finish CAS, epoch chains, boot reconciliation (FR-WS5)."""

from __future__ import annotations

import pytest


async def test_spawn_rejects_duplicates(registry):
    await registry.spawn_worker("w1", "/tmp/r", {})
    with pytest.raises(ValueError, match="already exists"):
        await registry.spawn_worker("w1", "/tmp/r", {})


async def test_claim_is_cas(registry):
    await registry.spawn_worker("w1", "/tmp/r", {})
    tid = await registry.enqueue_turn("w1", "hi")
    assert await registry.claim_turn(tid) is True
    assert await registry.claim_turn(tid) is False  # loser stays silent


async def test_finish_is_cas_and_accumulates_epoch_cost(registry):
    await registry.spawn_worker("w1", "/tmp/r", {})
    tid = await registry.enqueue_turn("w1", "hi")
    await registry.claim_turn(tid)
    await registry.start_turn(tid, None)
    assert await registry.finish_turn(tid, "done", session_id="s1", cost_usd=0.25) is True
    assert await registry.finish_turn(tid, "error", error="late") is False  # already terminal
    turn = await registry.get_turn(tid)
    assert turn["state"] == "done" and turn["session_id"] == "s1"
    epoch = await registry.current_epoch("w1")
    assert epoch["cost_usd"] == pytest.approx(0.25)


async def test_chain_tail_returns_latest_session_id(registry):
    await registry.spawn_worker("w1", "/tmp/r", {})
    epoch = await registry.current_epoch("w1")
    for sid in ("s1", "s2", "s3"):
        tid = await registry.enqueue_turn("w1", "p")
        await registry.claim_turn(tid)
        await registry.finish_turn(tid, "done", session_id=sid)
    assert await registry.chain_tail(epoch["id"]) == "s3"


async def test_roll_epoch_moves_current_and_resets_chain(registry):
    await registry.spawn_worker("w1", "/tmp/r", {})
    tid = await registry.enqueue_turn("w1", "p")
    await registry.claim_turn(tid)
    await registry.finish_turn(tid, "done", session_id="s1")
    new_epoch = await registry.roll_epoch("w1", "cycled")
    assert new_epoch["seq"] == 2
    assert await registry.chain_tail(new_epoch["id"]) is None  # fresh chain
    current = await registry.current_epoch("w1")
    assert current["id"] == new_epoch["id"]


async def test_boot_reconcile_redelivers_and_normalizes(registry):
    """AC-WS-4's code half: claimed/running turns redeliver; done turns don't."""
    await registry.spawn_worker("w1", "/tmp/r", {})
    done_id = await registry.enqueue_turn("w1", "finished")
    await registry.claim_turn(done_id)
    await registry.finish_turn(done_id, "done", session_id="s1")
    crashed_id = await registry.enqueue_turn("w1", "mid-flight")
    await registry.claim_turn(crashed_id)
    await registry.start_turn(crashed_id, "s1")
    await registry.park_question(crashed_id, "w1", [{"question": "?"}])
    await registry.set_worker_status("w1", "running")

    stats = await registry.boot_reconcile()

    assert stats == {"turns_redelivered": 1, "workers_normalized": 1, "questions_dismissed": 1}
    crashed = await registry.get_turn(crashed_id)
    assert crashed["state"] == "queued" and crashed["redeliveries"] == 1
    done = await registry.get_turn(done_id)
    assert done["state"] == "done"  # completed turns never re-run
    worker = await registry.get_worker("w1")
    assert worker["status"] == "idle"
    assert await registry.pending_questions() == []


async def test_question_resolution_is_cas(registry):
    await registry.spawn_worker("w1", "/tmp/r", {})
    tid = await registry.enqueue_turn("w1", "p")
    qid = await registry.park_question(tid, "w1", [{"question": "pick one"}])
    assert await registry.resolve_question(qid, "answered", "option A") is True
    assert await registry.resolve_question(qid, "timed_out", None) is False
