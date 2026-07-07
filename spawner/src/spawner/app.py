"""Spawner application wiring + lifecycle (ECA-65).

Boot order (AC#4 is load-bearing): connect -> bind the durable consumer -> **boot-reconcile every
local non-terminal record against container reality** -> only THEN start the pull loop + presence
heartbeat. Reconciliation before pulling new work IS the restart-recovery story (explicit, not
free).

The spawner is the sole NATS client on the peer; it owns the connection and relays. It never
provisions streams (the hub's ``ensure_bus`` does that) — it only creates its OWN
``spawner-<member>-<machine>`` durable consumer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import nats

from .config import Settings
from .consumer import SpawnerConsumer
from .launcher import DockerLauncher
from .presence import JsPublisher, PresenceHeartbeat
from .processor import JobProcessor
from .store import JobStore

logger = logging.getLogger(__name__)


async def connect(settings: Settings) -> Any:
    creds = settings.nats_creds_path
    return await nats.connect(
        servers=[settings.nats_url],
        name=f"spawner-{settings.member_id}-{settings.machine_id}",
        user_credentials=creds if creds else None,
        allow_reconnect=True,
        connect_timeout=5,
    )


class SpawnerApp:
    def __init__(self, settings: Settings):
        self._s = settings
        self._nc: Any = None
        self._store: JobStore | None = None
        self._stop = asyncio.Event()

    async def _reconcile(self, processor: JobProcessor) -> None:
        assert self._store is not None
        pending = await self._store.list_nonterminal()
        if pending:
            logger.info("boot reconciliation: %d non-terminal record(s)", len(pending))
        for rec in pending:
            try:
                await processor.reconcile_record(rec)
            except Exception:  # noqa: BLE001 — one bad record must not block the rest / the loop
                logger.exception("reconcile failed for job %s", rec.get("job_id"))

    async def run(self) -> None:
        s = self._s
        self._store = JobStore(s.db_file)
        await self._store.init()

        self._nc = await connect(s)
        js = self._nc.jetstream()
        logger.info("connected to NATS %s", s.nats_url)

        publisher = JsPublisher(js)
        launcher = DockerLauncher(s)
        processor = JobProcessor(s, self._store, launcher, publisher)
        consumer = SpawnerConsumer(s, js, processor)
        presence = PresenceHeartbeat(js, s.member_id, s.machine_id, s.presence_interval_s)

        await consumer.bind()
        # AC#4: reconcile BEFORE pulling any new work.
        await self._reconcile(processor)

        tasks = [
            asyncio.create_task(consumer.run(self._stop), name="pull-loop"),
            asyncio.create_task(presence.run(self._stop), name="presence"),
        ]
        logger.info("spawner running (member=%s machine=%s)", s.member_id, s.machine_id)
        try:
            await asyncio.gather(*tasks)
        finally:
            await self._shutdown(tasks)

    async def stop(self) -> None:
        self._stop.set()

    async def _shutdown(self, tasks: list[asyncio.Task]) -> None:
        self._stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:  # noqa: BLE001
                pass
        if self._store is not None:
            await self._store.close()
        logger.info("spawner stopped")
