"""Unit tests for the PRESENCE heartbeat + JsPublisher (ECA-65 AC#3, Q5).

The heartbeat writes ``{ts, status}`` to the PRESENCE KV key ``presence.<member>.<machine>`` and
must never crash the process on a KV hiccup (best-effort). These tests use a fake KV/JS.
"""

from __future__ import annotations

import asyncio
import json

from spawner.presence import JsPublisher, PresenceHeartbeat


class FakeKv:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    async def put(self, key: str, value: bytes) -> None:
        self.store[key] = value


class FakeJs:
    def __init__(self, kv=None, kv_raises=False):
        self._kv = kv or FakeKv()
        self._kv_raises = kv_raises
        self.published: list[tuple[str, bytes]] = []

    async def key_value(self, bucket: str):
        if self._kv_raises:
            raise RuntimeError("no bucket")
        return self._kv

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, data))


async def test_beat_writes_presence_key():
    kv = FakeKv()
    hb = PresenceHeartbeat(FakeJs(kv), "operator", "mini2", interval=10.0)
    await hb.beat()
    assert "presence.operator.mini2" in kv.store
    payload = json.loads(kv.store["presence.operator.mini2"])
    assert payload["status"] == "online"
    assert "ts" in payload


async def test_beat_custom_status():
    kv = FakeKv()
    hb = PresenceHeartbeat(FakeJs(kv), "operator", "mini2", interval=10.0)
    await hb.beat(status="draining")
    payload = json.loads(kv.store["presence.operator.mini2"])
    assert payload["status"] == "draining"


async def test_run_loop_beats_then_stops_on_event():
    kv = FakeKv()
    hb = PresenceHeartbeat(FakeJs(kv), "operator", "mini2", interval=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(hb.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert "presence.operator.mini2" in kv.store  # at least one beat landed


async def test_run_loop_survives_kv_error():
    # A KV hiccup must not take down the spawner: the loop swallows and keeps going until stop.
    hb = PresenceHeartbeat(FakeJs(kv_raises=True), "operator", "mini2", interval=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(hb.run(stop))
    await asyncio.sleep(0.03)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)  # did not raise despite every beat failing


async def test_js_publisher_delegates():
    js = FakeJs()
    pub = JsPublisher(js)
    await pub.publish("jobs.operator.j1.result", b"{}")
    assert js.published == [("jobs.operator.j1.result", b"{}")]
