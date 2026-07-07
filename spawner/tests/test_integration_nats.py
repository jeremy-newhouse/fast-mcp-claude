"""Live JetStream round-trip against a REAL nats-server (ECA-65 AC#6, ORCHESTRATOR NOTE).

This is the FIRST live JetStream round-trip in the system — ECA-66's NATS-path unit tests are
fake-backed (``_FakeJS``/``_FakeMsg``) by design, so they cannot prove JetStream accepts the exact
stream/consumer declarations or that the work-queue actually drains on ack. This test does, against
the ECA-63 stream declarations (provisioned inline here from the vendored ``bus_contract`` — the
drift backstop for the vendored copy: if the hub's names/policy ever diverge, this fails).

Skip-if-absent, same pattern as ``evolv-coder-agent/tests/test_bus.py``: no nats-server binary
(``NATS_SERVER_BIN`` env / PATH / the pinned ``/tmp/nats-server``) ⇒ the whole module SKIPS so a
box without it stays green.

Round-trip proven:
    provision JOBS/RESULTS/PRESENCE  (vendored bus_contract equivalent of ensure_bus)
    → spawner binds its durable ``spawner-<member>-<machine>`` consumer
    → ``js.publish("dispatch.<member>.<machine>", <job payload>)``
    → spawner pulls (real fetch)
    → container launch STUBBED to write result.json
    → asserts it publishes ``jobs.<member>.<job_id>.result`` (decode-compatible envelope)
    → asserts it ACKs (the JOBS work-queue drains to empty).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import nats
import pytest
from nats.js.api import KeyValueConfig, RetentionPolicy, StreamConfig

from spawner import bus_contract as bc
from spawner.config import Settings
from spawner.consumer import SpawnerConsumer
from spawner.presence import JsPublisher
from spawner.processor import JobProcessor
from spawner.store import DONE, JobStore

_PINNED = "/tmp/nats-server"
NATS_SERVER_BIN = (
    os.environ.get("NATS_SERVER_BIN")
    or shutil.which("nats-server")
    or (_PINNED if os.path.isfile(_PINNED) and os.access(_PINNED, os.X_OK) else None)
)

pytestmark = pytest.mark.skipif(
    not NATS_SERVER_BIN, reason="nats-server not installed (set NATS_SERVER_BIN or PATH)"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def nats_url():
    """Launch an ephemeral, loopback-bound ``nats-server -js`` for the duration of a test."""
    port = _free_port()
    store = tempfile.mkdtemp(prefix="spawner-natstest-")
    proc = subprocess.Popen(
        [NATS_SERVER_BIN, "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", store],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"nats://127.0.0.1:{port}"
    deadline = asyncio.get_event_loop().time() + 10.0
    while True:
        try:
            nc = await nats.connect(servers=[url], connect_timeout=1, allow_reconnect=False)
            await nc.close()
            break
        except Exception:  # noqa: BLE001 — server still starting
            if asyncio.get_event_loop().time() > deadline:
                proc.kill()
                raise RuntimeError("nats-server did not become ready in time") from None
            await asyncio.sleep(0.1)
    try:
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(store, ignore_errors=True)


async def _ensure_bus(js) -> None:
    """Provision JOBS/RESULTS/EVENTS + PRESENCE, mirroring the hub's ``bus.ensure_bus`` (ECA-63).

    Declared from the vendored ``bus_contract`` constants so a live server proves the spawner's
    durable consumer binds against the SAME shapes the hub uses (the vendored-copy drift backstop).
    """
    await js.add_stream(
        StreamConfig(name=bc.JOBS_STREAM, subjects=["dispatch.*.*"],
                     retention=RetentionPolicy.WORK_QUEUE)
    )
    await js.add_stream(
        StreamConfig(name=bc.RESULTS_STREAM, subjects=["jobs.*.*.result", "jobs.*.*.event"],
                     retention=RetentionPolicy.LIMITS)
    )
    await js.create_key_value(KeyValueConfig(bucket=bc.PRESENCE_BUCKET, history=1))


class _StubLauncher:
    """Stands in for DockerLauncher: writes a canned result.json instead of running a container."""

    def __init__(self, *, state="completed", text="live round-trip"):
        self.state = state
        self.text = text
        self.launched: list[str] = []

    async def launch(self, job_id, request, job_dir: Path) -> str:
        self.launched.append(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "events.jsonl").write_text(json.dumps({"type": "boot", "job_id": job_id}) + "\n")
        (job_dir / "result.json").write_text(
            json.dumps({"job_id": job_id, "state": self.state, "final_text": self.text,
                        "total_cost_usd": 0.02, "usage": {"input": 3}, "num_turns": 2})
        )
        return f"stub-cid-{job_id}"

    async def is_alive(self, container_id: str) -> bool:
        return False

    async def wait(self, container_id: str) -> int:
        return 0

    async def remove(self, container_id: str) -> None:
        pass


async def test_full_dispatch_to_result_roundtrip(nats_url, tmp_path):
    settings = Settings(
        _env_file=None, nats_url=nats_url, member_id="operator", machine_id="mini2",
        job_root=str(tmp_path / "jobs"), db_path=str(tmp_path / "spawner.db"),
    )
    job_id = "itest-1"
    result_subject = bc.result_subject(settings.member_id, job_id)

    nc = await nats.connect(servers=[nats_url])
    js = nc.jetstream()
    await _ensure_bus(js)

    # Capture the published .result via a core subscription established BEFORE dispatch.
    result_box: asyncio.Queue = asyncio.Queue()

    async def _on_result(msg):
        await result_box.put(msg.data)

    await nc.subscribe(result_subject, cb=_on_result)

    store = JobStore(settings.db_file)
    await store.init()
    launcher = _StubLauncher()
    processor = JobProcessor(settings, store, launcher, JsPublisher(js))
    consumer = SpawnerConsumer(settings, js, processor)
    await consumer.bind()  # binds spawner-operator-mini2 on dispatch.operator.mini2

    # Dispatch a job exactly as NatsDispatcher.dispatch would (job_id + derived-subject shape).
    dispatch_payload = {
        "job_id": job_id, "actor": "operator", "member": "operator", "machine": "mini2",
        "prompt": "prove the round-trip", "cwd": "/tmp",
        "limits": {"wall_clock": 60, "max_turns": 3, "max_budget_usd": 1.0},
    }
    await js.publish(
        bc.dispatch_subject("operator", "mini2"), json.dumps(dispatch_payload).encode()
    )

    # Real pull: the spawner fetches its own message off the live JOBS work-queue.
    msgs = await consumer._sub.fetch(batch=1, timeout=5)
    assert len(msgs) == 1
    await consumer._handle(msgs[0])  # process + publish .result + ack terminally

    try:
        # The derived .result landed, decode-compatible with the backend's _decode_result.
        raw = await asyncio.wait_for(result_box.get(), timeout=5)
        envelope = json.loads(raw)
        assert envelope["ok"] is True
        assert envelope["text"] == "live round-trip"
        assert envelope["total_cost_usd"] == 0.02
        assert launcher.launched == [job_id]

        # Local record is terminal.
        assert (await store.get(job_id))["state"] == DONE

        # The JOBS work-queue drained to empty (the message was acked + removed).
        jobs_info = await js.stream_info(bc.JOBS_STREAM)
        assert jobs_info.state.messages == 0

        # And nothing is left pending on the durable consumer.
        cinfo = await js.consumer_info(bc.JOBS_STREAM, bc.durable_name("operator", "mini2"))
        assert cinfo.num_pending == 0
        assert cinfo.num_ack_pending == 0
    finally:
        await store.close()
        await nc.drain()
