"""Tests for presence/roster and identity-based addressing (N-way peer mode)."""

import pytest

from fast_mcp_claude.services.store import Store
from fast_mcp_claude.tools.presence import who


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


# --------------------------------------------------- ECA-82: token-aware forget_presence


@pytest.mark.asyncio
async def test_forget_presence_token_match_deletes(store: Store):
    """A graceful shutdown forgets its OWN row when the token still matches."""
    await store.announce("eca2", metadata={"announce_token": "A"})
    deleted = await store.forget_presence("eca2", expected_token="A")
    assert deleted is True
    assert await store.list_presence() == []


@pytest.mark.asyncio
async def test_forget_presence_token_mismatch_is_noop(store: Store):
    """A stale/superseded process's forget must NEVER clobber a successor's row."""
    await store.announce("eca2", summary="new owner", metadata={"announce_token": "B"})
    deleted = await store.forget_presence("eca2", expected_token="A")
    assert deleted is False
    peers = await store.list_presence()
    assert len(peers) == 1
    assert peers[0]["metadata"]["announce_token"] == "B"


@pytest.mark.asyncio
async def test_forget_presence_tokenless_row_with_expected_token_is_noop(store: Store):
    """A legacy tokenless row has no token to match — an expected_token forget must not delete it
    (missing != any specific token, same "skip guard" direction as announce's own comparison)."""
    await store.announce("legacy", summary="pre-ECA-71 announcer")
    deleted = await store.forget_presence("legacy", expected_token="A")
    assert deleted is False
    assert len(await store.list_presence()) == 1


@pytest.mark.asyncio
async def test_forget_presence_missing_identity_with_token_is_noop(store: Store):
    deleted = await store.forget_presence("nobody", expected_token="A")
    assert deleted is False


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


@pytest.fixture
def wired_who(store: Store, monkeypatch):
    """who() reads the `store`/`settings` names bound in the presence tool module (imported
    once from ..server at import time), not a per-test instance -- point them at this test's
    isolated store/settings so who() observes what this test just announced."""
    monkeypatch.setattr("fast_mcp_claude.tools.presence.store", store)
    monkeypatch.setattr("fast_mcp_claude.tools.presence.settings", store.settings)
    return who


@pytest.mark.asyncio
async def test_who_redacts_announce_token(store: Store, wired_who):
    """FMC-2: who() must never expose a peer's announce_token (or other credential-shaped
    metadata key) over the wire, even though the row itself still carries it internally."""
    await store.announce(
        "eca2", summary="the real TUI", metadata={"announce_token": "A", "branch": "main"}
    )
    result = await wired_who()
    assert result["success"] is True
    assert len(result["peers"]) == 1
    peer = result["peers"][0]
    assert "announce_token" not in peer["metadata"]
    assert peer["metadata"] == {"branch": "main"}
    # The underlying store row is untouched -- forget()/announce()'s own guard reads it directly.
    raw = await store.list_presence()
    assert raw[0]["metadata"]["announce_token"] == "A"


@pytest.mark.asyncio
async def test_who_redact_guard_still_lets_reannounce_work(store: Store, wired_who):
    """AC #2: the forget-then-reannounce owner-token guard must keep working after the who()
    redaction -- it reads the token via its own raw query, never via the redacted tool output."""
    a = await store.announce("eca2", metadata={"announce_token": "A"})
    assert a["success"] is True

    refused = await store.announce("eca2", metadata={"announce_token": "B"})
    assert refused["success"] is False
    assert refused["error"]["code"] == "IDENTITY_LIVE_ELSEWHERE"

    deleted = await store.forget_presence("eca2", expected_token="A")
    assert deleted is True

    reannounced = await store.announce("eca2", metadata={"announce_token": "B"})
    assert reannounced["success"] is True

    result = await wired_who()
    assert "announce_token" not in result["peers"][0]["metadata"]


@pytest.mark.asyncio
async def test_who_redacts_nested_token(store: Store, wired_who):
    """AC #1 says "any peer credential" -- announce()'s metadata is arbitrary structured
    context, so a token buried in a nested dict must be redacted too, not just top-level."""
    await store.announce(
        "eca2",
        metadata={"announce_token": "A", "auth": {"refresh_token": "B", "scope": "read"}},
    )
    result = await wired_who()
    peer = result["peers"][0]
    assert "announce_token" not in peer["metadata"]
    assert "refresh_token" not in peer["metadata"]["auth"]
    assert peer["metadata"]["auth"] == {"scope": "read"}


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
