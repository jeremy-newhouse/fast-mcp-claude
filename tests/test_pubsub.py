"""Tests for pub/sub long-polling."""

import asyncio

import pytest

from fast_mcp_claude.services.store import Store


@pytest.mark.asyncio
async def test_publish_then_read(store: Store):
    mid = await store.publish("room1", "alice", {"text": "hi"})
    assert mid > 0
    msgs = await store.read_pubsub_after("room1", after_id=0)
    assert len(msgs) == 1
    assert msgs[0]["payload"] == {"text": "hi"}
    assert msgs[0]["channel"] == "room1"


@pytest.mark.asyncio
async def test_after_id_cursor(store: Store):
    id1 = await store.publish("room1", "alice", {"n": 1})
    id2 = await store.publish("room1", "alice", {"n": 2})
    msgs = await store.read_pubsub_after("room1", after_id=id1)
    assert len(msgs) == 1
    assert msgs[0]["id"] == id2


@pytest.mark.asyncio
async def test_subscribe_wakes_on_publish(store: Store):
    async def waiter():
        return await store.wait_for_pubsub("room1", after_id=0, timeout=5.0)

    async def publisher():
        await asyncio.sleep(0.05)
        return await store.publish("room1", "bob", {"hello": "world"})

    msgs, _id = await asyncio.gather(waiter(), publisher())
    assert len(msgs) == 1
    assert msgs[0]["payload"] == {"hello": "world"}


@pytest.mark.asyncio
async def test_subscribe_timeout_returns_empty(store: Store):
    msgs = await store.wait_for_pubsub("emptychannel", after_id=0, timeout=0.1)
    assert msgs == []


@pytest.mark.asyncio
async def test_other_channel_does_not_wake_subscribers(store: Store):
    """Publishing on channel B should not wake a subscriber on channel A."""

    async def waiter():
        return await store.wait_for_pubsub("A", after_id=0, timeout=0.3)

    async def publisher():
        await asyncio.sleep(0.05)
        return await store.publish("B", "x", {"n": 1})

    msgs, _ = await asyncio.gather(waiter(), publisher())
    assert msgs == []
