"""PRESENCE heartbeat + the JetStream-backed Publisher (ECA-65 AC#3, Q5 decision).

Two thin adapters over the live ``BusHandle``:

  * ``JsPublisher`` — the ``relay.Publisher`` impl: ``js.publish(subject, data)``. Used for the
    derived ``.result`` / ``.event`` subjects and the raw ``events.<member>.job.<state>``.
  * ``PresenceHeartbeat`` — writes ``{ts, status}`` to the PRESENCE KV key
    ``presence.<member>.<machine>`` every ``interval`` seconds (Q5: 10s heartbeat -> readers
    treat ``age > 3×`` = 30s as offline; the KV is history=1, no TTL — staleness is by
    timestamp, never a per-key TTL).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from . import bus_contract as bc

logger = logging.getLogger(__name__)


class JsPublisher:
    """``relay.Publisher`` backed by a live JetStream context."""

    def __init__(self, js: Any):
        self._js = js

    async def publish(self, subject: str, data: bytes) -> None:
        await self._js.publish(subject, data)


class PresenceHeartbeat:
    def __init__(self, js: Any, member: str, machine: str, interval: float):
        self._js = js
        self._key = bc.presence_key(member, machine)
        self._interval = interval
        self._kv: Any = None

    async def _ensure_kv(self) -> Any:
        if self._kv is None:
            self._kv = await self._js.key_value(bc.PRESENCE_BUCKET)
        return self._kv

    async def beat(self, status: str = "online") -> None:
        kv = await self._ensure_kv()
        payload = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "status": status}
        await kv.put(self._key, json.dumps(payload).encode("utf-8"))

    async def run(self, stop: asyncio.Event) -> None:
        """Heartbeat on the interval until ``stop`` is set (best-effort — never crash the loop)."""
        while not stop.is_set():
            try:
                await self.beat()
            except Exception:  # noqa: BLE001 — a KV hiccup must not take down the spawner
                logger.debug("presence heartbeat failed (non-fatal)", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
