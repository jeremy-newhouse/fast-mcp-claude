"""Tests for the SQLite store + long-poll notifier round-trip."""

import asyncio

import pytest

from fast_mcp_claude.services.store import (
    DECISION_ALLOW,
    DECISION_DENY,
    STATUS_DELIVERED,
    STATUS_REPLIED,
    Store,
)


@pytest.mark.asyncio
async def test_enqueue_then_pop(store: Store):
    msg_id = await store.enqueue_message(sender="alice", prompt="hi", recipient_session=None)
    popped = await store.pop_next_for_worker(recipient_session=None)
    assert popped is not None
    assert popped["id"] == msg_id
    assert popped["prompt"] == "hi"
    assert popped["status"] == STATUS_DELIVERED


@pytest.mark.asyncio
async def test_pop_returns_none_when_empty(store: Store):
    assert await store.pop_next_for_worker(recipient_session=None) is None


@pytest.mark.asyncio
async def test_empty_pop_survives_concurrent_commits(store: Store):
    """Regression: empty-inbox pop must not raise when another method commits
    concurrently on the shared connection.

    The launcher polls an empty inbox (pop_next_for_worker) while heartbeating
    announce() (a commit). When pop wrapped its SELECT in an explicit
    BEGIN IMMEDIATE, a concurrent commit() would commit pop's open transaction,
    so the empty-path ROLLBACK raised "cannot rollback - no transaction is
    active". Hammer the exact interleave; it must stay clean and keep returning
    None. (Reliably reproduced the old bug well under this iteration count.)"""
    iters = 500
    errors: list[str] = []

    async def popper():
        for _ in range(iters):
            try:
                assert await store.pop_next_for_worker("mini2_launcher") is None
            except Exception as exc:  # noqa: BLE001
                errors.append(f"pop raised: {type(exc).__name__}: {exc}")
                return

    async def committer():
        for i in range(iters):
            await store.announce("mini2_launcher", summary=f"beat {i}")
            await asyncio.sleep(0)

    await asyncio.gather(popper(), committer())
    assert not errors, errors[0]


@pytest.mark.asyncio
async def test_wait_for_instruction_wakes_on_enqueue(store: Store):
    """A blocked wait_for_next_for_worker should wake when a message is enqueued."""

    async def waiter():
        return await store.wait_for_next_for_worker(recipient_session=None, timeout=5.0)

    async def producer():
        await asyncio.sleep(0.05)
        return await store.enqueue_message(sender="bob", prompt="ping")

    waiter_task = asyncio.create_task(waiter())
    producer_task = asyncio.create_task(producer())

    result, msg_id = await asyncio.gather(waiter_task, producer_task)
    assert result is not None
    assert result["id"] == msg_id
    assert result["prompt"] == "ping"


@pytest.mark.asyncio
async def test_wait_for_instruction_timeout(store: Store):
    result = await store.wait_for_next_for_worker(recipient_session=None, timeout=0.1)
    assert result is None


@pytest.mark.asyncio
async def test_reply_round_trip(store: Store):
    msg_id = await store.enqueue_message(sender="alice", prompt="run x")
    assert await store.pop_next_for_worker(recipient_session=None) is not None

    async def waiter():
        return await store.wait_for_reply(msg_id, timeout=5.0)

    async def replier():
        await asyncio.sleep(0.05)
        return await store.record_reply(msg_id, "did it")

    reply_msg, _ok = await asyncio.gather(waiter(), replier())
    assert reply_msg is not None
    assert reply_msg["status"] == STATUS_REPLIED
    assert reply_msg["response"] == "did it"


@pytest.mark.asyncio
async def test_reply_to_unknown_message_returns_false(store: Store):
    assert await store.record_reply("0" * 32, "x") is False


@pytest.mark.asyncio
async def test_interrupt_flag(store: Store):
    assert await store.consume_interrupt("default") is False
    await store.request_interrupt("default")
    assert await store.consume_interrupt("default") is True
    # consume is one-shot
    assert await store.consume_interrupt("default") is False


@pytest.mark.asyncio
async def test_addressed_message_only_delivered_to_matching_session(store: Store):
    await store.enqueue_message(sender="a", prompt="for foo", recipient_session="foo")
    # A worker on a different session shouldn't see it.
    assert await store.pop_next_for_worker(recipient_session="bar") is None
    # The foo worker should.
    msg = await store.pop_next_for_worker(recipient_session="foo")
    assert msg is not None and msg["prompt"] == "for foo"


@pytest.mark.asyncio
async def test_broadcast_message_visible_to_any_worker(store: Store):
    await store.enqueue_message(sender="a", prompt="anyone?", recipient_session=None)
    msg = await store.pop_next_for_worker(recipient_session="foo")
    assert msg is not None and msg["prompt"] == "anyone?"


@pytest.mark.asyncio
async def test_approval_round_trip(store: Store):
    approval_id = await store.create_approval(
        session_id="default", tool_name="Bash", tool_input={"command": "ls"}
    )

    async def waiter():
        return await store.wait_for_approval_decision(approval_id, timeout=5.0)

    async def decider():
        await asyncio.sleep(0.05)
        return await store.decide_approval(approval_id, DECISION_ALLOW, "looks safe")

    approval, _ok = await asyncio.gather(waiter(), decider())
    assert approval is not None
    assert approval["decision"] == DECISION_ALLOW
    assert approval["reason"] == "looks safe"


@pytest.mark.asyncio
async def test_double_decision_rejected(store: Store):
    aid = await store.create_approval(session_id="s", tool_name="X", tool_input={})
    assert await store.decide_approval(aid, DECISION_ALLOW, None) is True
    assert await store.decide_approval(aid, DECISION_DENY, None) is False
