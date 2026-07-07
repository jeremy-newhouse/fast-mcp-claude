"""Durable pull consumer + pull loop (ECA-65 AC#1/#2).

Creates the durable pull consumer ``spawner-<member>-<machine>`` on the JOBS work-queue with the
EXACT policy the hub provisions (``bus_contract``): ``filter_subject=dispatch.<member>.<machine>``,
``ack_policy=explicit``, ``ack_wait=600``, ``max_deliver=3``, **no backoff list** (a backoff list
silently overrides AckWait). One consumer per machine ⇒ WorkQueuePolicy's one-consumer-per-subject
holds; a job for an offline spawner waits durably in the queue.

The pull loop mirrors ``ResultsBackend._run`` (``nats_dispatch.py``): ``fetch(batch=1, timeout=…)``
in a ``while not stopping`` loop, per-message ``try/except`` that leaves the msg **un-acked** on an
unexpected failure (redelivery is the retry). The heavy lifting is in ``JobProcessor`` — this file
is the thin plumbing: parse, authorize (own-only seam), delegate, ack TERMINALLY ONLY.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from nats.js.api import AckPolicy, ConsumerConfig

from . import bus_contract as bc
from .config import Settings
from .processor import SKIP, JobProcessor

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_S = 5


class SpawnerConsumer:
    def __init__(self, settings: Settings, js: Any, processor: JobProcessor):
        self._s = settings
        self._js = js
        self._proc = processor
        self._sub: Any = None

    def consumer_config(self) -> ConsumerConfig:
        return ConsumerConfig(
            durable_name=bc.durable_name(self._s.member_id, self._s.machine_id),
            filter_subject=bc.dispatch_subject(self._s.member_id, self._s.machine_id),
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=bc.JOBS_ACK_WAIT_S,
            max_deliver=bc.JOBS_MAX_DELIVER,
            backoff=None,  # NEVER set a backoff list — it silently overrides ack_wait
        )

    async def bind(self) -> None:
        """Create/attach the durable pull consumer (idempotent — durable re-bind on restart)."""
        subject = bc.dispatch_subject(self._s.member_id, self._s.machine_id)
        durable = bc.durable_name(self._s.member_id, self._s.machine_id)
        self._sub = await self._js.pull_subscribe(
            subject=subject,
            durable=durable,
            stream=bc.JOBS_STREAM,
            config=self.consumer_config(),
        )
        logger.info("bound durable pull consumer %s on %s", durable, subject)

    async def run(self, stop: asyncio.Event) -> None:
        """Pull loop: fetch one job at a time, process it, ack terminally only."""
        if self._sub is None:
            await self.bind()
        while not stop.is_set():
            try:
                msgs = await self._sub.fetch(batch=1, timeout=_FETCH_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception:
                # fetch timeout with no messages is the idle path, not an error.
                continue
            for msg in msgs:
                await self._handle(msg)

    async def _handle(self, msg: Any) -> None:
        try:
            payload = json.loads(msg.data)
        except (ValueError, TypeError):
            logger.warning("undecodable job payload; acking to drain the poison message")
            await msg.ack()
            return
        job_id = payload.get("job_id")
        if not job_id:
            logger.warning("job payload missing job_id; acking")
            await msg.ack()
            return
        if not self._authorized(payload):
            # Own-only spawn (D10): foreign actor -> publish error to .result, ack, stop.
            await self._reject_foreign(payload)
            await msg.ack()
            return
        try:
            action = await self._proc.process(payload, heartbeat=msg.in_progress)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Leave the message UN-ACKED so JetStream redelivers (broker-driven retry). The
            # terminal-state CAS + max_deliver=3 bound the re-runs.
            logger.exception("job %s failed; leaving un-acked for redelivery", job_id)
            return
        if action != SKIP:
            await msg.ack()  # ack TERMINALLY ONLY — the record is terminal + .result published

    def _authorized(self, payload: dict[str, Any]) -> bool:
        """Own-only spawn seam (D10). v0 with a fixed operator member: this machine only serves
        its own ``dispatch.<member>.<machine>`` subject, so a foreign machine can't reach us; the
        member is fixed. Kept as an explicit hook for the team phase."""
        machine = payload.get("machine")
        return machine is None or machine == self._s.machine_id

    async def _reject_foreign(self, payload: dict[str, Any]) -> None:
        from .relay import encode_capped, synthetic_error_envelope

        job_id = payload.get("job_id", "unknown")
        member = payload.get("member", self._s.member_id)
        envelope = synthetic_error_envelope(
            job_id, f"foreign machine {payload.get('machine')!r} not owned by this spawner"
        )
        try:
            await self._js.publish(
                bc.result_subject(member, job_id),
                encode_capped(envelope, self._s.result_inline_cap),
            )
        except Exception:  # noqa: BLE001
            logger.warning("failed publishing foreign-reject .result for job %s", job_id)
