"""Spawner-local durable job store + terminal-state CAS (ECA-65 AC#2, Q3 decision).

A NEW peer-local sqlite (NOT eCA's ``state.db`` — different machine; Q3 confirmed no reuse of
any fast-mcp-claude store). One table, ``jobs``, carrying the per-job state machine

    received -> launching -> running(container_id) -> terminal(done|error)

The compare-and-swap methods mirror ``evolv_coder_agent/state.py`` (``claim_job_delivery``): a
single ``UPDATE ... WHERE job_id=? AND state=<expected>`` whose ``rowcount == 1`` proves the
caller won the transition. This is what makes redelivery safe — the CAS keys on the TERMINAL
state, so a redelivered job whose local record is already terminal is a no-op (ack and stop),
and one whose record is non-terminal-but-container-dead relaunches (stateless-redo). One
container per job under redelivery.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

# State machine values.
RECEIVED = "received"
LAUNCHING = "launching"
RUNNING = "running"
DONE = "done"
ERROR = "error"
TERMINAL_STATES = frozenset({DONE, ERROR})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    member       TEXT NOT NULL,
    machine      TEXT NOT NULL,
    actor        TEXT,
    subject      TEXT,
    payload      TEXT,
    state        TEXT NOT NULL,
    container_id TEXT,
    result_text  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

_COLS = (
    "job_id, member, machine, actor, subject, payload, state, "
    "container_id, result_text, created_at, updated_at"
)


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class JobStore:
    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("JobStore not initialized - call init() first")
        return self._db

    async def claim(
        self,
        *,
        job_id: str,
        member: str,
        machine: str,
        actor: str | None,
        subject: str | None,
        payload: str | None,
    ) -> bool:
        """First-sighting claim: INSERT OR IGNORE a ``received`` row. Returns True iff THIS call
        inserted it (the winner proceeds to launch; a loser sees an existing record)."""
        now = utc_now_iso()
        cur = await self._conn().execute(
            f"INSERT OR IGNORE INTO jobs({_COLS}) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (job_id, member, machine, actor, subject, payload, RECEIVED, now, now),
        )
        won = cur.rowcount == 1
        await cur.close()
        await self._conn().commit()
        return won

    async def _cas(self, job_id: str, expected: str, new: str, **fields: Any) -> bool:
        """UPDATE state expected->new only if state currently == expected. rowcount==1 wins."""
        sets = ["state = ?", "updated_at = ?"]
        params: list[Any] = [new, utc_now_iso()]
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            params.append(v)
        params.extend([job_id, expected])
        cur = await self._conn().execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ? AND state = ?", params
        )
        won = cur.rowcount == 1
        await cur.close()
        await self._conn().commit()
        return won

    async def mark_launching(self, job_id: str) -> bool:
        return await self._cas(job_id, RECEIVED, LAUNCHING)

    async def mark_running(self, job_id: str, container_id: str) -> bool:
        return await self._cas(job_id, LAUNCHING, RUNNING, container_id=container_id)

    async def mark_terminal(self, job_id: str, *, ok: bool, result_text: str | None) -> bool:
        """Flip a job to a terminal state, storing its rendered result. CAS keys on NON-terminal:
        the transition wins only once, so an already-terminal record is a no-op (ack-and-stop)."""
        new = DONE if ok else ERROR
        cur = await self._conn().execute(
            "UPDATE jobs SET state = ?, result_text = ?, updated_at = ? "
            "WHERE job_id = ? AND state NOT IN (?, ?)",
            (new, result_text, utc_now_iso(), job_id, DONE, ERROR),
        )
        won = cur.rowcount == 1
        await cur.close()
        await self._conn().commit()
        return won

    async def set_container(self, job_id: str, container_id: str | None) -> None:
        """Record the container id without a state transition (relaunch re-attach bookkeeping)."""
        await self._conn().execute(
            "UPDATE jobs SET container_id = ?, updated_at = ? WHERE job_id = ?",
            (container_id, utc_now_iso(), job_id),
        )
        await self._conn().commit()

    async def get(self, job_id: str) -> dict[str, Any] | None:
        cur = await self._conn().execute(
            f"SELECT {_COLS} FROM jobs WHERE job_id = ?", (job_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return self._row(row) if row else None

    async def list_nonterminal(self) -> list[dict[str, Any]]:
        """Every job not yet terminal — the boot-reconciliation work list."""
        cur = await self._conn().execute(
            f"SELECT {_COLS} FROM jobs WHERE state NOT IN (?, ?) ORDER BY created_at ASC",
            (DONE, ERROR),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {
            "job_id": row[0],
            "member": row[1],
            "machine": row[2],
            "actor": row[3],
            "subject": row[4],
            "payload": row[5],
            "state": row[6],
            "container_id": row[7],
            "result_text": row[8],
            "created_at": row[9],
            "updated_at": row[10],
        }
