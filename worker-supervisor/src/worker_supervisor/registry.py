"""The worker registry: SQLite, the dedup + recovery authority (FR-WS5).

Discipline (lifted from eCA state.py / jobs.py, generalized):
- a turn record is MINTED (queued) before any subprocess spawns (Amendment A9);
- the engine CLAIMS it (queued->claimed, CAS) before spawning and records the
  terminal state after (claimed/running->terminal, CAS) — at-most-once execution
  per prompt within a run;
- boot reconciliation redelivers claimed-but-non-terminal turns (at-least-once
  across a crash). Worker prompts must tolerate redelivery.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

# Turn lifecycle. Terminal states record WHY the turn ended.
TURN_TERMINAL = (
    "done",
    "error",
    "timeout",
    "question_timeout",
    "budget_refused",
    "killed",
)
TURN_ACTIVE = ("queued", "claimed", "running")

WORKER_ACTIVE = ("idle", "running", "needs_input", "cycling")
WORKER_GONE = ("retired", "killed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    name            TEXT PRIMARY KEY,
    repo            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'idle',
    policy          TEXT NOT NULL DEFAULT '{}',
    current_epoch   INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_active_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS epochs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    worker      TEXT NOT NULL REFERENCES workers(name),
    seq         INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    end_reason  TEXT,
    cost_usd    REAL NOT NULL DEFAULT 0,
    UNIQUE (worker, seq)
);
CREATE TABLE IF NOT EXISTS turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch_id     INTEGER NOT NULL REFERENCES epochs(id),
    worker       TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'prompt',
    prompt       TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'queued',
    resume_from  TEXT,
    session_id   TEXT,
    result_text  TEXT,
    is_error     INTEGER,
    cost_usd     REAL,
    duration_ms  INTEGER,
    num_turns    INTEGER,
    usage       TEXT,
    tools       TEXT,
    error        TEXT,
    redeliveries INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    claimed_at   TEXT,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_worker_state ON turns (worker, state);
CREATE INDEX IF NOT EXISTS idx_turns_epoch ON turns (epoch_id);
CREATE TABLE IF NOT EXISTS questions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id        INTEGER NOT NULL REFERENCES turns(id),
    worker         TEXT NOT NULL,
    questions      TEXT NOT NULL,
    answer         TEXT,
    state          TEXT NOT NULL DEFAULT 'pending',
    asked_at       TEXT NOT NULL,
    resolved_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_questions_state ON questions (state);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _row(r: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


class Registry:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Registry.connect() not called"
        return self._db

    # -- workers ------------------------------------------------------------

    async def spawn_worker(self, name: str, repo: str, policy: dict[str, Any]) -> dict[str, Any]:
        """Create a worker (idle) with its first epoch. Rejects duplicates."""
        now = _now()
        existing = await self.get_worker(name)
        if existing is not None:
            raise ValueError(f"worker {name!r} already exists (status={existing['status']})")
        await self.db.execute(
            "INSERT INTO workers (name, repo, status, policy, created_at, updated_at,"
            " last_active_at) VALUES (?, ?, 'idle', ?, ?, ?, ?)",
            (name, repo, json.dumps(policy), now, now, now),
        )
        epoch_id = await self._open_epoch(name, seq=1)
        await self.db.execute(
            "UPDATE workers SET current_epoch = ? WHERE name = ?", (epoch_id, name)
        )
        await self.db.commit()
        return (await self.get_worker(name))  # type: ignore[return-value]

    async def get_worker(self, name: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM workers WHERE name = ?", (name,))
        return _row(await cur.fetchone())

    async def list_workers(self, include_gone: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM workers"
        if not include_gone:
            q += f" WHERE status NOT IN ({','.join('?' * len(WORKER_GONE))})"
            cur = await self.db.execute(q + " ORDER BY name", WORKER_GONE)
        else:
            cur = await self.db.execute(q + " ORDER BY name")
        return [dict(r) for r in await cur.fetchall()]

    async def set_worker_status(self, name: str, status: str, *, active: bool = False) -> None:
        now = _now()
        if active:
            await self.db.execute(
                "UPDATE workers SET status = ?, updated_at = ?, last_active_at = ? WHERE name = ?",
                (status, now, now, name),
            )
        else:
            await self.db.execute(
                "UPDATE workers SET status = ?, updated_at = ? WHERE name = ?",
                (status, now, name),
            )
        await self.db.commit()

    async def delete_worker(self, name: str) -> bool:
        """Purge a worker and ALL its history (questions -> turns -> epochs -> row).

        Children first because foreign_keys=ON. Returns False if no such worker.
        Frees the workers.name PRIMARY KEY so the name can be re-spawned (ECA-99).
        """
        if await self.get_worker(name) is None:
            return False
        await self.db.execute("DELETE FROM questions WHERE worker = ?", (name,))
        await self.db.execute("DELETE FROM turns WHERE worker = ?", (name,))
        await self.db.execute("DELETE FROM epochs WHERE worker = ?", (name,))
        await self.db.execute("DELETE FROM workers WHERE name = ?", (name,))
        await self.db.commit()
        return True

    # -- epochs ---------------------------------------------------------------

    async def _open_epoch(self, worker: str, seq: int) -> int:
        cur = await self.db.execute(
            "INSERT INTO epochs (worker, seq, started_at) VALUES (?, ?, ?)",
            (worker, seq, _now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    async def current_epoch(self, worker: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT e.* FROM epochs e JOIN workers w ON w.current_epoch = e.id"
            " WHERE w.name = ?",
            (worker,),
        )
        return _row(await cur.fetchone())

    async def roll_epoch(self, worker: str, reason: str) -> dict[str, Any]:
        """End the current epoch (reason) and open the next one."""
        now = _now()
        epoch = await self.current_epoch(worker)
        assert epoch is not None, f"worker {worker!r} has no current epoch"
        await self.db.execute(
            "UPDATE epochs SET ended_at = ?, end_reason = ? WHERE id = ?",
            (now, reason, epoch["id"]),
        )
        new_id = await self._open_epoch(worker, seq=epoch["seq"] + 1)
        await self.db.execute(
            "UPDATE workers SET current_epoch = ?, updated_at = ? WHERE name = ?",
            (new_id, now, worker),
        )
        await self.db.commit()
        cur = await self.db.execute("SELECT * FROM epochs WHERE id = ?", (new_id,))
        return _row(await cur.fetchone())  # type: ignore[return-value]

    async def end_epoch(self, epoch_id: int, reason: str) -> None:
        await self.db.execute(
            "UPDATE epochs SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL",
            (_now(), reason, epoch_id),
        )
        await self.db.commit()

    async def chain_tail(self, epoch_id: int) -> str | None:
        """Last recorded session id in an epoch — the resume target."""
        cur = await self.db.execute(
            "SELECT session_id FROM turns WHERE epoch_id = ? AND session_id IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            (epoch_id,),
        )
        r = await cur.fetchone()
        return r["session_id"] if r else None

    # -- turns ----------------------------------------------------------------

    async def enqueue_turn(self, worker: str, prompt: str, kind: str = "prompt") -> int:
        """Mint the turn record BEFORE anything spawns (Amendment A9)."""
        epoch = await self.current_epoch(worker)
        assert epoch is not None, f"worker {worker!r} has no current epoch"
        cur = await self.db.execute(
            "INSERT INTO turns (epoch_id, worker, kind, prompt, state, created_at)"
            " VALUES (?, ?, ?, ?, 'queued', ?)",
            (epoch["id"], worker, kind, prompt, _now()),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def next_queued_turn(self, worker: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM turns WHERE worker = ? AND state = 'queued' ORDER BY id LIMIT 1",
            (worker,),
        )
        return _row(await cur.fetchone())

    async def claim_turn(self, turn_id: int) -> bool:
        """CAS queued -> claimed. The winner spawns the subprocess."""
        cur = await self.db.execute(
            "UPDATE turns SET state = 'claimed', claimed_at = ? WHERE id = ?"
            " AND state = 'queued'",
            (_now(), turn_id),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def start_turn(self, turn_id: int, resume_from: str | None) -> None:
        await self.db.execute(
            "UPDATE turns SET state = 'running', started_at = ?, resume_from = ?"
            " WHERE id = ? AND state = 'claimed'",
            (_now(), resume_from, turn_id),
        )
        await self.db.commit()

    async def finish_turn(
        self,
        turn_id: int,
        state: str,
        *,
        session_id: str | None = None,
        result_text: str | None = None,
        is_error: bool | None = None,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
        num_turns: int | None = None,
        usage: dict[str, Any] | None = None,
        tools: list[str] | None = None,
        error: str | None = None,
    ) -> bool:
        """CAS active -> terminal; persists session id + telemetry atomically (G4).

        Returns False if the turn was not in an active state (already finished).
        """
        assert state in TURN_TERMINAL, f"not a terminal state: {state}"
        cur = await self.db.execute(
            "UPDATE turns SET state = ?, session_id = ?, result_text = ?, is_error = ?,"
            " cost_usd = ?, duration_ms = ?, num_turns = ?, usage = ?, tools = ?,"
            " error = ?, finished_at = ?"
            " WHERE id = ? AND state IN ('claimed', 'running')",
            (
                state,
                session_id,
                result_text,
                None if is_error is None else int(is_error),
                cost_usd,
                duration_ms,
                num_turns,
                json.dumps(usage) if usage is not None else None,
                json.dumps(tools) if tools is not None else None,
                error,
                _now(),
                turn_id,
            ),
        )
        won = cur.rowcount == 1
        if won and cost_usd:
            await self.db.execute(
                "UPDATE epochs SET cost_usd = cost_usd + ? WHERE id ="
                " (SELECT epoch_id FROM turns WHERE id = ?)",
                (cost_usd, turn_id),
            )
        await self.db.commit()
        return won

    async def get_turn(self, turn_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM turns WHERE id = ?", (turn_id,))
        return _row(await cur.fetchone())

    async def last_finished_turn(self, worker: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            f"SELECT * FROM turns WHERE worker = ? AND state IN"
            f" ({','.join('?' * len(TURN_TERMINAL))}) ORDER BY id DESC LIMIT 1",
            (worker, *TURN_TERMINAL),
        )
        return _row(await cur.fetchone())

    async def history(self, worker: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if worker:
            cur = await self.db.execute(
                "SELECT * FROM turns WHERE worker = ? ORDER BY id DESC LIMIT ?",
                (worker, limit),
            )
        else:
            cur = await self.db.execute("SELECT * FROM turns ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    # -- questions ------------------------------------------------------------

    async def park_question(self, turn_id: int, worker: str, questions: Any) -> int:
        cur = await self.db.execute(
            "INSERT INTO questions (turn_id, worker, questions, asked_at)"
            " VALUES (?, ?, ?, ?)",
            (turn_id, worker, json.dumps(questions), _now()),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def pending_questions(self, worker: str | None = None) -> list[dict[str, Any]]:
        if worker:
            cur = await self.db.execute(
                "SELECT * FROM questions WHERE state = 'pending' AND worker = ? ORDER BY id",
                (worker,),
            )
        else:
            cur = await self.db.execute(
                "SELECT * FROM questions WHERE state = 'pending' ORDER BY id"
            )
        return [dict(r) for r in await cur.fetchall()]

    async def resolve_question(self, question_id: int, state: str, answer: str | None) -> bool:
        """CAS pending -> answered/timed_out/dismissed."""
        assert state in ("answered", "timed_out", "dismissed")
        cur = await self.db.execute(
            "UPDATE questions SET state = ?, answer = ?, resolved_at = ?"
            " WHERE id = ? AND state = 'pending'",
            (state, answer, _now(), question_id),
        )
        await self.db.commit()
        return cur.rowcount == 1

    # -- boot reconciliation (FR-WS5) ------------------------------------------

    async def boot_reconcile(self) -> dict[str, int]:
        """Redeliver claimed-but-non-terminal turns; normalize worker states.

        Crash mid-turn leaves turns in claimed/running: reset them to queued
        (redelivery counter bumped) so the engine re-runs them. Workers stuck in
        transient states drop back to idle; their queues redeliver naturally.
        """
        cur = await self.db.execute(
            "UPDATE turns SET state = 'queued', redeliveries = redeliveries + 1,"
            " claimed_at = NULL, started_at = NULL WHERE state IN ('claimed', 'running')"
        )
        redelivered = cur.rowcount
        cur = await self.db.execute(
            "UPDATE workers SET status = 'idle', updated_at = ?"
            " WHERE status IN ('running', 'needs_input', 'cycling')",
            (_now(),),
        )
        normalized = cur.rowcount
        # A pending question's turn is being redelivered; the old parked question
        # can never be answered into a live stream anymore.
        cur = await self.db.execute(
            "UPDATE questions SET state = 'dismissed', resolved_at = ? WHERE state = 'pending'",
            (_now(),),
        )
        dismissed = cur.rowcount
        await self.db.commit()
        return {
            "turns_redelivered": redelivered,
            "workers_normalized": normalized,
            "questions_dismissed": dismissed,
        }
