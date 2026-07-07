"""Unit tests for the spawner-local job store + terminal-state CAS (ECA-65 AC#2)."""

from __future__ import annotations

import pytest

from spawner.store import DONE, ERROR, RECEIVED, RUNNING, JobStore


@pytest.fixture
async def store(tmp_path):
    s = JobStore(tmp_path / "spawner.db")
    await s.init()
    yield s
    await s.close()


async def _claim(store, job_id="job1"):
    return await store.claim(
        job_id=job_id,
        member="operator",
        machine="mini2",
        actor="alice",
        subject="dispatch.operator.mini2",
        payload="{}",
    )


async def test_claim_is_idempotent(store):
    assert await _claim(store) is True
    # A redelivery / duplicate claim loses.
    assert await _claim(store) is False
    row = await store.get("job1")
    assert row["state"] == RECEIVED


async def test_happy_path_transitions(store):
    await _claim(store)
    assert await store.mark_launching("job1") is True
    assert await store.mark_running("job1", "container-abc") is True
    row = await store.get("job1")
    assert row["state"] == RUNNING
    assert row["container_id"] == "container-abc"
    assert await store.mark_terminal("job1", ok=True, result_text="done") is True
    row = await store.get("job1")
    assert row["state"] == DONE
    assert row["result_text"] == "done"


async def test_cas_rejects_wrong_expected_state(store):
    await _claim(store)
    # Cannot mark_running before launching (expected=LAUNCHING, actual=RECEIVED).
    assert await store.mark_running("job1", "c1") is False
    row = await store.get("job1")
    assert row["state"] == RECEIVED


async def test_terminal_cas_wins_once(store):
    await _claim(store)
    await store.mark_launching("job1")
    assert await store.mark_terminal("job1", ok=True, result_text="first") is True
    # Second flip (redelivery after terminal) is a no-op — ack-and-stop.
    assert await store.mark_terminal("job1", ok=False, result_text="second") is False
    row = await store.get("job1")
    assert row["state"] == DONE
    assert row["result_text"] == "first"


async def test_terminal_from_running_marks_error(store):
    await _claim(store)
    await store.mark_launching("job1")
    await store.mark_running("job1", "c1")
    assert await store.mark_terminal("job1", ok=False, result_text="boom") is True
    row = await store.get("job1")
    assert row["state"] == ERROR


async def test_list_nonterminal_excludes_terminal(store):
    await _claim(store, "a")
    await _claim(store, "b")
    await store.mark_launching("a")
    await store.mark_running("a", "ca")
    await _claim(store, "c")
    await store.mark_launching("c")
    await store.mark_terminal("c", ok=True, result_text="ok")
    ids = {r["job_id"] for r in await store.list_nonterminal()}
    assert ids == {"a", "b"}  # c is terminal, excluded
    states = {r["job_id"]: r["state"] for r in await store.list_nonterminal()}
    assert states["a"] == RUNNING
    assert states["b"] == RECEIVED


async def test_set_container_no_state_change(store):
    await _claim(store)
    await store.set_container("job1", "cid-1")
    row = await store.get("job1")
    assert row["container_id"] == "cid-1"
    assert row["state"] == RECEIVED
    await store.set_container("job1", None)
    row = await store.get("job1")
    assert row["container_id"] is None
