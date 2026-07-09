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


# ------------------------------------------------------- ECA-71 owner-token identity guard
# ADR-0029 Layer B: a channel sidecar is the SOLE announcer for its identity. A second live
# process reusing the identity (a claude.ai background fork of the TUI session) used to clobber
# presence and both would claim the same mailbox (misroute / black hole). The store now refuses a
# second announcer whose owner token differs while the first is still heartbeating.


@pytest.mark.asyncio
async def test_announce_refuses_second_live_process(store: Store):
    ok = await store.announce("eca2", summary="the real TUI", metadata={"announce_token": "A"})
    assert ok["success"] is True

    refused = await store.announce("eca2", summary="a fork", metadata={"announce_token": "B"})
    assert refused["success"] is False
    assert refused["error"]["code"] == "IDENTITY_LIVE_ELSEWHERE"

    # The fork did NOT clobber the row — the real owner's presence stands.
    peers = await store.list_presence()
    assert len(peers) == 1
    assert peers[0]["summary"] == "the real TUI"
    assert peers[0]["metadata"]["announce_token"] == "A"


@pytest.mark.asyncio
async def test_announce_same_token_reannounces(store: Store):
    """The owner heartbeats with a stable token — every beat must be accepted (not self-refused)."""
    a = await store.announce("eca2", summary="beat 1", metadata={"announce_token": "A"})
    b = await store.announce("eca2", summary="beat 2", metadata={"announce_token": "A"})
    assert a["success"] is True and b["success"] is True
    peers = await store.list_presence()
    assert peers[0]["summary"] == "beat 2"


@pytest.mark.asyncio
async def test_announce_tokenless_never_refused(store: Store):
    """Backward compatibility: a pre-ECA-71 announcer sends no token and is never guarded."""
    a = await store.announce("legacy", summary="first")
    b = await store.announce("legacy", summary="second")
    assert a["success"] is True and b["success"] is True
    # Even against an existing TOKENED row, a tokenless announce is accepted (missing => skip).
    await store.announce("eca2", metadata={"announce_token": "A"})
    c = await store.announce("eca2", summary="tokenless takeover")
    assert c["success"] is True


@pytest.mark.asyncio
async def test_announce_stale_token_reclaimed(store: Store):
    """A dead announcer's token goes stale (heartbeat lapses) and a new process reclaims it —
    so crash-and-relaunch and legitimate takeover still work."""
    await store.announce("eca2", summary="old owner", metadata={"announce_token": "A"})
    # Backdate the heartbeat well beyond the freshness window (poll_heartbeat_s*3 = 6s here).
    await store.db.execute(
        "UPDATE presence SET updated_at = updated_at - 10000 WHERE identity = ?", ("eca2",)
    )
    await store.db.commit()

    reclaimed = await store.announce("eca2", summary="new owner", metadata={"announce_token": "B"})
    assert reclaimed["success"] is True
    peers = await store.list_presence()
    assert peers[0]["summary"] == "new owner"
    assert peers[0]["metadata"]["announce_token"] == "B"


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
