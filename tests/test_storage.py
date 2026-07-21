"""Tests for the SQLite store + long-poll notifier round-trip."""

import asyncio

import pytest

from fast_mcp_claude.services.store import (
    DECISION_ALLOW,
    DECISION_DENY,
    STATUS_DELIVERED,
    STATUS_EXPIRED,
    STATUS_REPLIED,
    Notifier,
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
async def test_broadcast_wakes_identity_scoped_waiter(store: Store):
    """Regression (FMC-5 AC#1): a worker long-polling on its own identity key must
    wake immediately when a broadcast (recipient_session=None) message arrives,
    not just after the full poll timeout — pop_next_for_worker("foo") does return
    NULL-recipient rows, but the old enqueue_message only notified the wildcard
    inbox:* key, never inbox:foo."""

    async def waiter():
        return await store.wait_for_next_for_worker(recipient_session="foo", timeout=5.0)

    async def producer():
        await asyncio.sleep(0.05)
        return await store.enqueue_message(
            sender="bob", prompt="broadcast ping", recipient_session=None
        )

    waiter_task = asyncio.create_task(waiter())
    producer_task = asyncio.create_task(producer())

    result, msg_id = await asyncio.gather(waiter_task, producer_task)
    assert result is not None
    assert result["id"] == msg_id
    assert result["prompt"] == "broadcast ping"


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


@pytest.mark.asyncio
async def test_list_messages_filters_by_recipient_session(store: Store):
    """The live-session sidecar relies on this exact, index-backed per-identity filter
    so a busy hub can't push its messages out of the newest-N window (notify-loss)."""
    await store.enqueue_message(sender="brain", prompt="for A", recipient_session="mini2.a")
    await store.enqueue_message(sender="brain", prompt="for B", recipient_session="mini2.b")
    await store.enqueue_message(sender="brain", prompt="broadcast", recipient_session=None)
    only_a = await store.list_messages(status="queued", recipient_session="mini2.a")
    assert [m["prompt"] for m in only_a] == ["for A"]
    # unfiltered still returns the whole queue (backward compatible)
    assert len(await store.list_messages(status="queued")) == 3


# ------------------------------------------------------------- Notifier (FMC-5)


@pytest.mark.asyncio
async def test_wait_for_continues_after_lost_race():
    """Regression (FMC-5 AC#3): if check() returns None on wakeup (lost a race to
    another waiter), wait_for must keep waiting out the remaining timeout budget
    instead of returning early."""
    notifier = Notifier()
    calls: list[int] = []

    async def check():
        calls.append(1)
        if len(calls) < 3:
            return None
        return "done"

    task = asyncio.create_task(notifier.wait_for("k", check, timeout=2.0))
    await asyncio.sleep(0.02)
    notifier.notify("k")  # first wake: check() still returns None (lost race)
    await asyncio.sleep(0.02)
    notifier.notify("k")  # second wake: check() finally succeeds

    result = await task
    assert result == "done"
    assert len(calls) == 3  # initial check + 2 post-wakeup re-checks


@pytest.mark.asyncio
async def test_wait_for_genuine_timeout_still_returns_none():
    """A wait that never gets notified must still return None at (approximately)
    the requested timeout, not early and not hang forever."""
    notifier = Notifier()

    async def always_empty():
        return None

    start = asyncio.get_event_loop().time()
    result = await notifier.wait_for("k", always_empty, timeout=0.1)
    elapsed = asyncio.get_event_loop().time() - start
    assert result is None
    assert elapsed >= 0.1


@pytest.mark.asyncio
async def test_notifier_forget_does_not_evict_an_active_waiter():
    """Safety property for FMC-5 AC#2's eviction: forget() must not pull the
    Event out from under a waiter currently parked on that key."""
    notifier = Notifier()

    async def always_empty():
        return None

    waiter_task = asyncio.create_task(notifier.wait_for("k", always_empty, timeout=1.0))
    await asyncio.sleep(0.05)  # let the waiter register and start waiting
    assert "k" in notifier._events

    notifier.forget("k")
    assert "k" in notifier._events  # still parked; must not be evicted

    result = await waiter_task
    assert result is None  # times out normally afterward

    notifier.forget("k")
    assert "k" not in notifier._events  # now safe to evict


@pytest.mark.asyncio
async def test_zero_timeout_wait_for_does_not_leak_notifier_event():
    """Regression (FMC-12 AC#3): wait_for's timeout<=0 short-circuit must never
    register a persistent Event -- pre-fix, _get(key) ran unconditionally before the
    timeout check, so every zero-timeout call against a fresh key (e.g. an attacker
    probing a fabricated inbox:/pubsub: key) permanently grew _events even though the
    caller never actually waits on it."""
    notifier = Notifier()

    async def always_empty():
        return None

    for i in range(50):
        result = await notifier.wait_for(f"probe:{i}", always_empty, timeout=0)
        assert result is None

    assert len(notifier._events) == 0


@pytest.mark.asyncio
async def test_wait_for_instruction_zero_timeout_does_not_leak_notifier_events(store: Store):
    """Regression (FMC-12 AC#3), exercised through the real production path: an
    authenticated caller repeatedly invoking wait_for_instruction (wait_for_next_
    for_worker) with fresh, syntactically-valid but nonexistent recipient_session
    values and timeout=0 must not grow the Notifier's event map -- recipient_session
    is validated only for SESSION_RE format, never for corresponding to a live
    identity, so this is the exact adversarial pattern the task describes."""
    before = len(store._notifier._events)
    for i in range(50):
        result = await store.wait_for_next_for_worker(f"fabricated-session-{i}", timeout=0)
        assert result is None
    assert len(store._notifier._events) == before


@pytest.mark.asyncio
async def test_subscribe_zero_timeout_does_not_leak_notifier_events(store: Store):
    """Regression (FMC-12 AC#3): same adversarial pattern via subscribe/wait_for_pubsub
    with fresh, nonexistent channel names -- validate_channel is format-only too."""
    before = len(store._notifier._events)
    for i in range(50):
        msgs = await store.wait_for_pubsub(f"fabricated-channel-{i}", after_id=0, timeout=0)
        assert msgs == []
    assert len(store._notifier._events) == before


@pytest.mark.asyncio
async def test_notifier_events_bounded_by_max_events_cap():
    """Regression (FMC-12 AC#3): even when a caller uses a nonzero timeout (so an
    Event genuinely gets registered), Notifier._events must stay bounded rather than
    growing forever as an unbounded number of distinct fabricated keys accumulate --
    the least-recently-used, un-waited keys are evicted once over capacity."""
    notifier = Notifier(max_events=5)
    for i in range(20):
        notifier._get(f"inbox:fake-{i}")
    assert len(notifier._events) <= 5
    # The most-recently-created keys must be the ones that survived (LRU eviction).
    assert "inbox:fake-19" in notifier._events
    assert "inbox:fake-0" not in notifier._events


@pytest.mark.asyncio
async def test_notifier_eviction_never_evicts_an_active_waiter():
    """Safety property for the FMC-12 AC#3 capacity eviction: it must respect the
    same active-waiter guard forget() already honors -- a key a real, live session is
    currently long-polling on must never be evicted just because many fabricated keys
    pushed the map over capacity."""
    notifier = Notifier(max_events=3)

    async def always_empty():
        return None

    waiter_task = asyncio.create_task(notifier.wait_for("keep", always_empty, timeout=1.0))
    await asyncio.sleep(0.05)  # let the waiter register and start waiting
    assert "keep" in notifier._events

    for i in range(20):
        notifier._get(f"filler:{i}")

    assert "keep" in notifier._events  # never evicted despite being over capacity

    result = await waiter_task
    assert result is None  # times out normally afterward


@pytest.mark.asyncio
async def test_cleanup_evicts_resolved_message_notifier_key(store: Store):
    """Regression (FMC-5 AC#2): Notifier._events must not grow without bound as
    messages are resolved and pruned over a long-running process."""
    msg_id = await store.enqueue_message(sender="a", prompt="hi")
    await store.pop_next_for_worker(recipient_session=None)

    async def waiter():
        return await store.wait_for_reply(msg_id, timeout=5.0)

    async def replier():
        await asyncio.sleep(0.05)
        return await store.record_reply(msg_id, "done")

    await asyncio.gather(waiter(), replier())
    key = store._outbox_key(msg_id)
    assert key in store._notifier._events  # lingers after being waited on

    await store.db.execute("UPDATE messages SET created_at=? WHERE id=?", (1000.0, msg_id))
    await store._cleanup_once(cutoff=2000.0)

    assert await store.get_message(msg_id) is None  # pruned
    assert key not in store._notifier._events  # and its notifier key evicted


@pytest.mark.asyncio
async def test_expired_message_survives_one_cleanup_cycle_before_deletion(store: Store):
    """Regression (FMC-5 AC#4): a row marked expired this sweep must be observable
    (status="expired") for at least one more sweep before it is pruned — the old
    code deleted it in the very same pass that marked it, so wait_for_completion()
    callers only ever saw NotFoundError, never status="expired"."""
    msg_id = await store.enqueue_message(sender="a", prompt="orphaned")
    await store.db.execute("UPDATE messages SET created_at=? WHERE id=?", (1800.0, msg_id))

    # Sweep 1: crosses the expire threshold, but the grace period keeps it alive.
    await store._cleanup_once(cutoff=2000.0, delete_grace=500.0)
    msg = await store.get_message(msg_id)
    assert msg is not None
    assert msg["status"] == STATUS_EXPIRED

    # Sweep 2: now old enough (relative to the advanced delete cutoff) to prune.
    await store._cleanup_once(cutoff=2500.0, delete_grace=500.0)
    assert await store.get_message(msg_id) is None


@pytest.mark.asyncio
async def test_cleanup_without_grace_keeps_legacy_same_pass_delete(store: Store):
    """Default delete_grace=0.0 preserves the pre-fix same-pass mark+delete
    behavior for direct callers that don't pass a grace period."""
    msg_id = await store.enqueue_message(sender="a", prompt="orphaned")
    await store.db.execute("UPDATE messages SET created_at=? WHERE id=?", (1000.0, msg_id))
    await store._cleanup_once(cutoff=2000.0)
    assert await store.get_message(msg_id) is None
