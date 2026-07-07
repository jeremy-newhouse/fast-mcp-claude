"""Unit tests for the (re)delivery decision table + orchestration (ECA-65 AC#2/#3/#4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spawner.config import Settings
from spawner.processor import ACK, ATTACH, LAUNCH, RELAUNCH, JobProcessor
from spawner.store import DONE, ERROR, JobStore


class FakeLauncher:
    """Writes a canned result.json on launch; liveness + exit code are configurable."""

    def __init__(self, *, state="completed", exit_code=0, alive_after_launch=False):
        self.state = state
        self.exit_code = exit_code
        self.alive_after_launch = alive_after_launch
        self.alive: dict[str, bool] = {}
        self.launched: list[str] = []
        self.removed: list[str] = []
        self._seq = 0

    async def launch(self, job_id, request, job_dir: Path) -> str:
        self.launched.append(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "events.jsonl").write_text(json.dumps({"type": "boot"}) + "\n")
        (job_dir / "result.json").write_text(
            json.dumps(
                {"job_id": job_id, "state": self.state, "final_text": "the answer",
                 "total_cost_usd": 0.01, "usage": {"in": 1}, "num_turns": 1}
            )
        )
        self._seq += 1
        cid = f"cid-{job_id}-{self._seq}"
        self.alive[cid] = self.alive_after_launch
        return cid

    async def is_alive(self, container_id: str) -> bool:
        return self.alive.get(container_id, False)

    async def wait(self, container_id: str) -> int:
        self.alive[container_id] = False
        return self.exit_code

    async def remove(self, container_id: str) -> None:
        self.removed.append(container_id)
        self.alive[container_id] = False


class FakePublisher:
    def __init__(self):
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, data))

    def subjects(self):
        return [s for s, _ in self.published]

    def result_payloads(self):
        return [json.loads(d) for s, d in self.published if s.endswith(".result")]


@pytest.fixture
async def store(tmp_path):
    s = JobStore(tmp_path / "spawner.db")
    await s.init()
    yield s
    await s.close()


def _settings(tmp_path) -> Settings:
    return Settings(job_root=str(tmp_path / "jobs"), db_path=str(tmp_path / "spawner.db"))


def _payload(job_id="job1"):
    return {
        "job_id": job_id, "actor": "alice", "member": "operator", "machine": "mini2",
        "prompt": "do it", "limits": {"wall_clock": 60, "max_turns": 5, "max_budget_usd": 1.0},
    }


async def test_fresh_job_launches_and_publishes_result(tmp_path, store):
    s = _settings(tmp_path)
    launcher = FakeLauncher()
    pub = FakePublisher()
    proc = JobProcessor(s, store, launcher, pub)

    action = await proc.process(_payload())
    assert action == ACK
    assert launcher.launched == ["job1"]
    # A .result was published on the derived subject, decode-compatible.
    results = pub.result_payloads()
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["text"] == "the answer"
    assert results[0]["total_cost_usd"] == 0.01
    assert "jobs.operator.job1.result" in pub.subjects()
    # Local record is terminal.
    rec = await store.get("job1")
    assert rec["state"] == DONE


async def test_terminal_record_is_ack_and_stop(tmp_path, store):
    s = _settings(tmp_path)
    launcher = FakeLauncher()
    proc = JobProcessor(s, store, launcher, FakePublisher())
    # Seed a terminal record.
    await store.claim(job_id="job1", member="operator", machine="mini2", actor="a",
                      subject=None, payload=json.dumps(_payload()))
    await store.mark_launching("job1")
    await store.mark_terminal("job1", ok=True, result_text="prior")

    assert await proc.decide("job1") == ACK
    action = await proc.process(_payload())
    assert action == ACK
    assert launcher.launched == []  # never re-launched


async def test_nonterminal_dead_container_relaunches(tmp_path, store):
    s = _settings(tmp_path)
    launcher = FakeLauncher()
    proc = JobProcessor(s, store, launcher, FakePublisher())
    # Seed a running record whose container is dead (crashed mid-run).
    await store.claim(job_id="job1", member="operator", machine="mini2", actor="a",
                      subject=None, payload=json.dumps(_payload()))
    await store.mark_launching("job1")
    await store.mark_running("job1", "dead-cid")
    # dead-cid is not registered alive in the launcher.

    assert await proc.decide("job1") == RELAUNCH
    action = await proc.process(_payload())
    assert action == ACK
    assert "dead-cid" in launcher.removed  # stale container force-removed
    assert launcher.launched == ["job1"]  # relaunched exactly once
    rec = await store.get("job1")
    assert rec["state"] == DONE


async def test_error_state_when_no_result_json(tmp_path, store):
    s = _settings(tmp_path)

    class NoResultLauncher(FakeLauncher):
        async def launch(self, job_id, request, job_dir: Path) -> str:
            job_dir.mkdir(parents=True, exist_ok=True)
            self.launched.append(job_id)
            return f"cid-{job_id}"  # writes NO result.json

    launcher = NoResultLauncher(exit_code=137)
    pub = FakePublisher()
    proc = JobProcessor(s, store, launcher, pub)
    action = await proc.process(_payload())
    assert action == ACK
    results = pub.result_payloads()
    assert results[0]["ok"] is False
    assert "no result.json" in results[0]["error"]
    rec = await store.get("job1")
    assert rec["state"] == ERROR


async def test_heartbeat_invoked_during_run(tmp_path, store):
    s = _settings(tmp_path)
    s.presence_interval_s = 0.001  # force the heartbeat interval tiny
    calls = []

    class SlowLauncher(FakeLauncher):
        async def wait(self, container_id: str) -> int:
            import asyncio
            await asyncio.sleep(0.05)
            return 0

    proc = JobProcessor(s, store, SlowLauncher(), FakePublisher())

    async def hb():
        calls.append(1)

    await proc.process(_payload(), heartbeat=hb)
    assert len(calls) >= 1  # in_progress was pinged during the wait


async def test_decide_fresh_is_launch(tmp_path, store):
    proc = JobProcessor(_settings(tmp_path), store, FakeLauncher(), FakePublisher())
    assert await proc.decide("never-seen") == LAUNCH


async def test_reconcile_dead_container_publishes_terminal_error(tmp_path, store):
    s = _settings(tmp_path)
    pub = FakePublisher()
    proc = JobProcessor(s, store, FakeLauncher(), pub)
    await store.claim(job_id="job1", member="operator", machine="mini2", actor="a",
                      subject=None, payload=json.dumps(_payload()))
    await store.mark_launching("job1")
    await store.mark_running("job1", "gone-cid")
    rec = await store.get("job1")

    await proc.reconcile_record(rec)
    results = pub.result_payloads()
    assert results[0]["ok"] is False
    assert "restarted with no live container" in results[0]["error"]
    assert (await store.get("job1"))["state"] == ERROR


async def test_reconcile_live_container_attaches(tmp_path, store):
    s = _settings(tmp_path)
    launcher = FakeLauncher()
    # Register a live container + pre-write its result.json (as if the runner finished).
    job_dir = Path(s.job_root_dir) / "job1"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "result.json").write_text(
        json.dumps({"job_id": "job1", "state": "completed", "final_text": "attached"})
    )
    launcher.alive["live-cid"] = True
    pub = FakePublisher()
    proc = JobProcessor(s, store, launcher, pub)
    await store.claim(job_id="job1", member="operator", machine="mini2", actor="a",
                      subject=None, payload=json.dumps(_payload()))
    await store.mark_launching("job1")
    await store.mark_running("job1", "live-cid")
    rec = await store.get("job1")

    await proc.reconcile_record(rec)
    assert launcher.launched == []  # attached, did not launch a second container
    results = pub.result_payloads()
    assert results[0]["text"] == "attached"
    assert (await store.get("job1"))["state"] == DONE


async def test_attach_action_does_not_relaunch(tmp_path, store):
    s = _settings(tmp_path)
    launcher = FakeLauncher()
    launcher.alive["live-cid"] = True
    job_dir = Path(s.job_root_dir) / "job1"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "result.json").write_text(
        json.dumps({"job_id": "job1", "state": "completed", "final_text": "x"})
    )
    proc = JobProcessor(s, store, launcher, FakePublisher())
    await store.claim(job_id="job1", member="operator", machine="mini2", actor="a",
                      subject=None, payload=json.dumps(_payload()))
    await store.mark_launching("job1")
    await store.mark_running("job1", "live-cid")
    assert await proc.decide("job1") == ATTACH
    await proc.process(_payload())
    assert launcher.launched == []
