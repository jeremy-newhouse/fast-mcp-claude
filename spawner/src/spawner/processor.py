"""Per-job orchestration: the (re)delivery decision table + launch/relay/publish (ECA-65 AC#2/#3).

``JobProcessor`` carries the whole per-job decision so it is unit-testable against a fake
launcher + fake publisher (the "logic in a testable method, thin plumbing" split the fleet uses
throughout). The consumer's NATS pull loop (``consumer.py``) is the thin plumbing that feeds it
one message at a time and acks TERMINALLY ONLY.

Decision table on (re)delivery (docs §JetStream delivery semantics, finding 3):

  * local record TERMINAL          -> ``ack`` (idempotent; the prior run already published .result)
  * local record non-terminal,
      container ALIVE               -> ``attach`` (wait it out + publish; one container per job)
      container DEAD/absent         -> ``relaunch`` (stateless-redo — a "launched=no-op" CAS would
                                        black-hole exactly the crashed-mid-run jobs)
  * no local record                -> ``launch`` (claim received -> launching -> run)

Terminal-state CAS keys on TERMINAL, not "launched": the message is acked only after the local
record reaches terminal AND the ``.result`` is published.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from . import bus_contract as bc
from .config import Settings
from .launcher import ContainerLauncher, build_request
from .relay import (
    EventTailer,
    Publisher,
    build_result_envelope,
    encode_capped,
    read_result,
    synthetic_error_envelope,
)
from .store import RUNNING, TERMINAL_STATES, JobStore

logger = logging.getLogger(__name__)

# Decision actions.
ACK = "ack"
ATTACH = "attach"
RELAUNCH = "relaunch"
LAUNCH = "launch"
SKIP = "skip"


class JobProcessor:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        launcher: ContainerLauncher,
        publisher: Publisher,
    ):
        self._s = settings
        self._store = store
        self._launcher = launcher
        self._pub = publisher

    def job_dir(self, job_id: str) -> Path:
        return self._s.job_root_dir / job_id

    async def decide(self, job_id: str) -> str:
        """Pure(ish) decision: inspect the local record + container liveness, return an action."""
        rec = await self._store.get(job_id)
        if rec is None:
            return LAUNCH
        if rec["state"] in TERMINAL_STATES:
            return ACK
        container_id = rec["container_id"]
        if container_id and await self._launcher.is_alive(container_id):
            return ATTACH
        return RELAUNCH

    async def process(self, payload: dict[str, Any], heartbeat: Any = None) -> str:
        """Handle one delivered job. Returns an action string; the caller acks iff not ``SKIP``.

        ``heartbeat`` is an optional async callable (``msg.in_progress``) invoked periodically so
        AckWait (600s) never fires mid pull/clone/cold-start.
        """
        job_id = payload["job_id"]
        action = await self.decide(job_id)
        logger.info("job %s: decision=%s", job_id, action)

        if action == ACK:
            return ACK  # already terminal — idempotent ack-and-stop

        if action == LAUNCH:
            won = await self._store.claim(
                job_id=job_id,
                member=payload.get("member", self._s.member_id),
                machine=payload.get("machine", self._s.machine_id),
                actor=payload.get("actor"),
                subject=payload.get("subject"),
                payload=json.dumps(payload),
            )
            if not won:
                # A concurrent delivery already claimed it; re-decide once (likely ATTACH/ACK).
                action = await self.decide(job_id)
                if action in (ACK, SKIP):
                    return action

        rec = await self._store.get(job_id)
        if rec and rec["state"] in TERMINAL_STATES:
            return ACK

        return await self._run(payload, action, heartbeat)

    async def _run(self, payload: dict[str, Any], action: str, heartbeat: Any) -> str:
        job_id = payload["job_id"]
        member = payload.get("member", self._s.member_id)
        machine = payload.get("machine", self._s.machine_id)
        job_dir = self.job_dir(job_id)

        # RELAUNCH: force-remove any stale container so we never run two per job.
        if action == RELAUNCH:
            rec = await self._store.get(job_id)
            if rec and rec["container_id"]:
                await self._launcher.remove(rec["container_id"])
                await self._store.set_container(job_id, None)

        stop = asyncio.Event()
        hb_task = asyncio.create_task(self._heartbeat_loop(stop, heartbeat)) if heartbeat else None
        tailer = EventTailer(
            job_dir, self._pub, bc.event_subject(member, job_id), self._s.event_inline_cap
        )
        tail_task: asyncio.Task | None = None
        try:
            if action == ATTACH:
                rec = await self._store.get(job_id)
                container_id = rec["container_id"] if rec else None
            else:
                await self._store.mark_launching(job_id)
                request = build_request(payload)
                container_id = await self._launcher.launch(job_id, request, job_dir)
                await self._store.mark_running(job_id, container_id)

            tail_task = asyncio.create_task(tailer.run_until(stop))
            exit_code = await self._launcher.wait(container_id) if container_id else -1
        finally:
            stop.set()
            if tail_task is not None:
                await tail_task
            if hb_task is not None:
                await hb_task

        envelope = self._result_envelope(job_id, job_dir, exit_code)
        await self._publish_result(member, job_id, envelope)
        await self._store.mark_terminal(
            job_id, ok=bool(envelope["ok"]), result_text=envelope.get("text")
        )
        await self._publish_job_event(member, machine, envelope)
        logger.info("job %s: terminal ok=%s exit=%s", job_id, envelope["ok"], exit_code)
        return ACK

    def _result_envelope(self, job_id: str, job_dir: Path, exit_code: int) -> dict[str, Any]:
        result_json = read_result(job_dir)
        if result_json is not None:
            return build_result_envelope(result_json)
        # No result.json: the runner never produced a terminal frame (crash / OOM-kill / bad exit).
        # A result is never silently dropped — synthesize an error terminal (AC#5 spirit).
        return synthetic_error_envelope(
            job_id, f"no result.json produced (container exit={exit_code})"
        )

    async def _publish_result(self, member: str, job_id: str, envelope: dict[str, Any]) -> None:
        data = encode_capped(envelope, self._s.result_inline_cap)
        await self._pub.publish(bc.result_subject(member, job_id), data)

    async def _publish_job_event(
        self, member: str, machine: str, envelope: dict[str, Any]
    ) -> None:
        """Best-effort raw fleet event on EVENTS (hub re-publisher scrubs to events.team.*)."""
        state = "done" if envelope["ok"] else "error"
        body = json.dumps(
            {"job_id": envelope.get("job_id"), "state": envelope.get("state")}
        ).encode()
        try:
            await self._pub.publish(bc.job_event_subject(member, machine, state), body)
        except Exception:  # noqa: BLE001 — event stream is best-effort; never fail a job on it
            logger.debug("job-state event publish failed (non-fatal)", exc_info=True)

    async def _heartbeat_loop(self, stop: asyncio.Event, heartbeat: Any) -> None:
        interval = min(self._s.presence_interval_s * 3, bc.JOBS_ACK_WAIT_S / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                try:
                    await heartbeat()
                except Exception:  # noqa: BLE001
                    logger.debug("in_progress heartbeat failed (non-fatal)", exc_info=True)

    async def reconcile_record(self, rec: dict[str, Any]) -> None:
        """Boot reconciliation for ONE local non-terminal record (AC#4).

        * running(container_id) alive  -> re-attach + wait + publish terminal.
        * running/launching, container gone -> publish a terminal error .result so the backend
          isn't left waiting (JetStream will also redeliver; the terminal CAS keeps it one-shot).
        """
        job_id = rec["job_id"]
        member = rec["member"]
        machine = rec["machine"]
        payload = json.loads(rec["payload"]) if rec.get("payload") else {"job_id": job_id}
        container_id = rec["container_id"]

        if rec["state"] == RUNNING and container_id and await self._launcher.is_alive(container_id):
            logger.info("boot-reconcile: re-attaching to live container for job %s", job_id)
            await self._run({**payload, "member": member, "machine": machine}, ATTACH, None)
            return

        logger.info(
            "boot-reconcile: job %s (%s) has no live container — publishing terminal error",
            job_id, rec["state"],
        )
        envelope = synthetic_error_envelope(
            job_id, f"spawner restarted with no live container (was {rec['state']})"
        )
        await self._publish_result(member, job_id, envelope)
        await self._store.mark_terminal(job_id, ok=False, result_text=envelope["text"])
        await self._publish_job_event(member, machine, envelope)
