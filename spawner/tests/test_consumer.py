"""Unit tests for the durable pull-consumer plumbing (ECA-65 AC#1/#2).

The consumer is deliberately thin (the decision logic lives in ``JobProcessor``), so these tests
pin the plumbing invariants: the consumer config matches the vendored contract byte-for-byte,
poison messages are drained (acked) not redelivered forever, a foreign machine is rejected with a
published ``.result`` then acked, a processing exception leaves the message UN-acked (JetStream
redelivery is the retry), and a terminal outcome acks TERMINALLY ONLY.
"""

from __future__ import annotations

import json

from nats.js.api import AckPolicy

from spawner import bus_contract as bc
from spawner.config import Settings
from spawner.consumer import SpawnerConsumer
from spawner.processor import ACK, SKIP


class FakeMsg:
    def __init__(self, data: bytes):
        self.data = data
        self.acked = 0
        self.in_progress_calls = 0

    async def ack(self) -> None:
        self.acked += 1

    async def in_progress(self) -> None:
        self.in_progress_calls += 1


class FakeProcessor:
    def __init__(self, *, action=ACK, raises=False):
        self.action = action
        self.raises = raises
        self.processed: list[dict] = []

    async def process(self, payload, heartbeat=None):
        self.processed.append(payload)
        if self.raises:
            raise RuntimeError("boom")
        return self.action


class FakeJs:
    def __init__(self):
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, data))


def _settings() -> Settings:
    return Settings(member_id="operator", machine_id="mini2")


def _consumer(proc, js=None) -> SpawnerConsumer:
    return SpawnerConsumer(_settings(), js or FakeJs(), proc)


def test_consumer_config_matches_contract():
    cfg = _consumer(FakeProcessor()).consumer_config()
    assert cfg.durable_name == "spawner-operator-mini2"
    assert cfg.filter_subject == "dispatch.operator.mini2"
    assert cfg.ack_policy == AckPolicy.EXPLICIT
    assert cfg.ack_wait == bc.JOBS_ACK_WAIT_S == 600
    assert cfg.max_deliver == bc.JOBS_MAX_DELIVER == 3
    assert cfg.backoff is None  # a backoff list silently overrides ack_wait — never set one


async def test_terminal_action_acks_once():
    proc = FakeProcessor(action=ACK)
    consumer = _consumer(proc)
    msg = FakeMsg(json.dumps({"job_id": "job1", "machine": "mini2"}).encode())
    await consumer._handle(msg)
    assert msg.acked == 1
    assert proc.processed and proc.processed[0]["job_id"] == "job1"


async def test_skip_action_does_not_ack():
    consumer = _consumer(FakeProcessor(action=SKIP))
    msg = FakeMsg(json.dumps({"job_id": "job1"}).encode())
    await consumer._handle(msg)
    assert msg.acked == 0  # SKIP means "leave for another delivery / no terminal yet"


async def test_processing_exception_leaves_unacked_for_redelivery():
    consumer = _consumer(FakeProcessor(raises=True))
    msg = FakeMsg(json.dumps({"job_id": "job1"}).encode())
    await consumer._handle(msg)
    assert msg.acked == 0  # un-acked -> JetStream redelivers (broker-driven retry)


async def test_poison_payload_is_acked_to_drain():
    consumer = _consumer(FakeProcessor())
    msg = FakeMsg(b"not json{{{")
    await consumer._handle(msg)
    assert msg.acked == 1  # poison message drained, not redelivered forever


async def test_missing_job_id_is_acked():
    consumer = _consumer(FakeProcessor())
    msg = FakeMsg(json.dumps({"prompt": "hi"}).encode())
    await consumer._handle(msg)
    assert msg.acked == 1


async def test_foreign_machine_rejected_and_acked():
    js = FakeJs()
    proc = FakeProcessor()
    consumer = _consumer(proc, js=js)
    msg = FakeMsg(json.dumps({"job_id": "job9", "machine": "other-box"}).encode())
    await consumer._handle(msg)
    assert msg.acked == 1  # own-only spawn (D10): rejected + acked, never processed
    assert proc.processed == []
    subjects = [s for s, _ in js.published]
    assert "jobs.operator.job9.result" in subjects
    envelope = json.loads(js.published[0][1])
    assert envelope["ok"] is False
    assert "foreign machine" in envelope["error"]


async def test_heartbeat_passed_to_processor():
    captured = {}

    class CapturingProcessor(FakeProcessor):
        async def process(self, payload, heartbeat=None):
            captured["hb"] = heartbeat
            return ACK

    consumer = _consumer(CapturingProcessor())
    msg = FakeMsg(json.dumps({"job_id": "j", "machine": "mini2"}).encode())
    await consumer._handle(msg)
    # The consumer wires msg.in_progress as the AckWait heartbeat.
    assert captured["hb"] == msg.in_progress
