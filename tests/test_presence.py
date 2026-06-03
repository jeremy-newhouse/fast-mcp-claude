"""Tests for presence/roster and identity-based addressing (N-way peer mode)."""

import pytest

from fast_mcp_claude.services.store import Store


@pytest.mark.asyncio
async def test_announce_then_list(store: Store):
    await store.announce("alice", summary="refactoring auth", metadata={"branch": "main"})
    peers = await store.list_presence()
    assert len(peers) == 1
    p = peers[0]
    assert p["identity"] == "alice"
    assert p["summary"] == "refactoring auth"
    assert p["metadata"] == {"branch": "main"}
    assert p["age_seconds"] >= 0


@pytest.mark.asyncio
async def test_announce_upserts(store: Store):
    await store.announce("alice", summary="first")
    await store.announce("alice", summary="second")
    peers = await store.list_presence()
    assert len(peers) == 1
    assert peers[0]["summary"] == "second"


@pytest.mark.asyncio
async def test_list_presence_stale_filter(store: Store):
    await store.announce("fresh", summary="now")
    await store.announce("stale", summary="old")
    # Backdate 'stale' well beyond the freshness window.
    await store.db.execute(
        "UPDATE presence SET updated_at = updated_at - 10000 WHERE identity = ?",
        ("stale",),
    )
    await store.db.commit()

    fresh_only = await store.list_presence(stale_after=100.0)
    assert [p["identity"] for p in fresh_only] == ["fresh"]

    everyone = await store.list_presence(stale_after=None)
    assert {p["identity"] for p in everyone} == {"fresh", "stale"}


@pytest.mark.asyncio
async def test_forget_presence(store: Store):
    await store.announce("alice")
    await store.forget_presence("alice")
    assert await store.list_presence() == []


@pytest.mark.asyncio
async def test_identity_addressed_message_routing(store: Store):
    """A message addressed to 'bob' lands in bob's mailbox, not alice's; an
    unaddressed (broadcast) message reaches anyone. This is the N-way contract
    that lets different developers/sessions talk to a specific peer by identity."""
    await store.enqueue_message(sender="ctrl", prompt="for bob", recipient_session="bob")

    # Alice does not receive bob's addressed message...
    assert await store.pop_next_for_worker("alice") is None
    # ...but bob does.
    got = await store.pop_next_for_worker("bob")
    assert got is not None and got["prompt"] == "for bob"

    # Broadcast reaches the next idle worker regardless of identity.
    await store.enqueue_message(sender="ctrl", prompt="anyone", recipient_session=None)
    got2 = await store.pop_next_for_worker("alice")
    assert got2 is not None and got2["prompt"] == "anyone"
