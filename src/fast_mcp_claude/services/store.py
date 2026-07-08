"""SQLite-backed persistent store for the remote-control server.

Tables:
    messages   — controller → worker prompts; worker replies via response field
    approvals  — PreToolUse hook requests waiting for a controller decision
    pubsub     — broadcast channel messages (subscribers track their own cursor)
    interrupts — pending interrupt flags per session

All long-polling tools go through this module:
    wait_for_instruction / wait_for_completion / await_decision / subscribe
each call `wait_for(key, timeout)` and re-check the DB on wakeup. Cross-process
or cross-event-loop notifications are NOT supported (single asyncio loop per
server process) — that's fine because each peer machine runs its own server.
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import aiosqlite

from ..config import Settings
from ..logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# Message lifecycle
STATUS_QUEUED = "queued"  # in inbox, no worker has picked up yet
STATUS_DELIVERED = "delivered"  # wait_for_instruction handed it to a worker
STATUS_REPLIED = "replied"  # worker called reply()
STATUS_CANCELLED = "cancelled"  # controller called interrupt() or similar
STATUS_EXPIRED = "expired"  # TTL cleanup

# Approval lifecycle
DECISION_ALLOW = "allow"
DECISION_DENY = "deny"

# Teams-outbox lifecycle (ADR-0013): a peer live session asks the hub to post to Teams.
OUTBOX_PENDING = "pending"  # created, awaiting the hub controller to drain + post
OUTBOX_DONE = "done"  # the hub posted (or failed) and completed the request


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id                  TEXT PRIMARY KEY,
    sender              TEXT NOT NULL,
    recipient_session   TEXT,
    prompt              TEXT NOT NULL,
    metadata            TEXT,
    status              TEXT NOT NULL,
    response            TEXT,
    created_at          REAL NOT NULL,
    delivered_at        REAL,
    replied_at          REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_recipient
    ON messages(recipient_session, status, created_at);

CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    tool_input      TEXT NOT NULL,
    decision        TEXT,
    reason          TEXT,
    created_at      REAL NOT NULL,
    decided_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_pending ON approvals(decision, created_at);

CREATE TABLE IF NOT EXISTS teams_outbox (
    id              TEXT PRIMARY KEY,
    requester       TEXT NOT NULL,
    target          TEXT,
    text            TEXT NOT NULL,
    metadata        TEXT,
    status          TEXT NOT NULL,
    ok              INTEGER,
    detail          TEXT,
    created_at      REAL NOT NULL,
    completed_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_teams_outbox_pending ON teams_outbox(status, created_at);

CREATE TABLE IF NOT EXISTS session_relay (
    id              TEXT PRIMARY KEY,
    requester       TEXT NOT NULL,
    op              TEXT NOT NULL,
    payload         TEXT,
    status          TEXT NOT NULL,
    ok              INTEGER,
    result          TEXT,
    created_at      REAL NOT NULL,
    completed_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_session_relay_pending ON session_relay(status, created_at);

CREATE TABLE IF NOT EXISTS pubsub (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,
    sender      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pubsub_channel ON pubsub(channel, id);

CREATE TABLE IF NOT EXISTS interrupts (
    session_id      TEXT PRIMARY KEY,
    requested_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS presence (
    identity    TEXT PRIMARY KEY,
    summary     TEXT,
    metadata    TEXT,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_presence_updated ON presence(updated_at);
"""


class Notifier:
    """Per-key asyncio.Event registry for waking long-poll waiters.

    On notify(): set the existing event (waking any current waiters) and
    replace it with a fresh one so subsequent waits block again.
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def _get(self, key: str) -> asyncio.Event:
        ev = self._events.get(key)
        if ev is None:
            ev = asyncio.Event()
            self._events[key] = ev
        return ev

    def notify(self, key: str) -> None:
        ev = self._events.get(key)
        if ev is not None:
            ev.set()
            self._events[key] = asyncio.Event()

    async def wait_for(
        self,
        key: str,
        check: Callable[[], Awaitable[T | None]],
        timeout: float,
    ) -> T | None:
        """Long-poll pattern: check DB now; if empty, wait on the event then re-check.

        Returns the first non-None result from `check()`, or None on timeout.
        """
        # Capture the event reference BEFORE checking DB so we don't miss a
        # notification that arrives between check and wait.
        ev = self._get(key)

        result = await check()
        if result is not None:
            return result

        if timeout <= 0:
            return None

        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

        return await check()


class Store:
    """SQLite store + notification hub. One instance per server process."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()
        self._notifier = Notifier()
        self._cleanup_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        path = self.settings.db_full_path
        # isolation_level=None (autocommit): every method here is a single-
        # statement write (no method opens an explicit transaction — see
        # pop_next_for_worker for why claim-uniqueness rides on _db_lock instead).
        # In autocommit each write applies immediately, so on this single shared
        # connection there is never an open transaction for a concurrent commit()
        # to interfere with. The remaining db.commit() calls are harmless no-ops.
        self._db = await aiosqlite.connect(str(path), isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

    async def close(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store not initialized")
        return self._db

    # ------------------------------------------------------------------ messages

    async def enqueue_message(
        self,
        sender: str,
        prompt: str,
        recipient_session: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        message_id = uuid.uuid4().hex
        now = time.time()
        await self.db.execute(
            "INSERT INTO messages "
            "(id, sender, recipient_session, prompt, metadata, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                sender,
                recipient_session,
                prompt,
                json.dumps(metadata) if metadata else None,
                STATUS_QUEUED,
                now,
            ),
        )
        await self.db.commit()
        # Wake any wait_for_instruction blocked on this recipient (or wildcard).
        self._notifier.notify(self._inbox_key(recipient_session))
        if recipient_session is not None:
            self._notifier.notify(self._inbox_key(None))
        return message_id

    async def pop_next_for_worker(self, recipient_session: str | None) -> dict[str, Any] | None:
        """Atomically claim the oldest queued message addressed to this worker.

        A NULL recipient_session message is delivered to ANY worker (broadcast).
        A worker calling with session="foo" gets either messages addressed to
        "foo" OR unaddressed (NULL) ones.

        Atomicity note: claim-uniqueness (no two workers grab the same row) is
        provided by `_db_lock` — the single-process mutex this class already
        relies on for record_reply/cancel/cleanup. We deliberately do NOT wrap
        the SELECT+UPDATE in an explicit BEGIN IMMEDIATE/ROLLBACK. This is a
        single shared aiosqlite connection: `commit()` is global, so a concurrent
        commit() from an UNLOCKED writer (announce/enqueue/publish/...) would
        commit this method's open transaction out from under it, making the
        empty-path ROLLBACK misfire with "cannot rollback - no transaction is
        active" (the exact failure the launcher hit polling an empty inbox while
        heartbeating announce). In autocommit mode (isolation_level=None) each
        statement below applies immediately; no statement here opens a
        transaction, so nothing can misfire and no stray commit() can corrupt it.
        """
        async with self._db_lock:
            if recipient_session is None:
                cur = await self.db.execute(
                    "SELECT * FROM messages WHERE status=? AND recipient_session IS NULL "
                    "ORDER BY created_at ASC LIMIT 1",
                    (STATUS_QUEUED,),
                )
            else:
                cur = await self.db.execute(
                    "SELECT * FROM messages WHERE status=? AND "
                    "(recipient_session=? OR recipient_session IS NULL) "
                    "ORDER BY created_at ASC LIMIT 1",
                    (STATUS_QUEUED, recipient_session),
                )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                return None

            now = time.time()
            await self.db.execute(
                "UPDATE messages SET status=?, delivered_at=? WHERE id=?",
                (STATUS_DELIVERED, now, row["id"]),
            )
            await self.db.commit()
            msg = _row_to_message(row, delivered_at=now)
            msg["status"] = STATUS_DELIVERED
            return msg

    async def wait_for_next_for_worker(
        self,
        recipient_session: str | None,
        timeout: float,
    ) -> dict[str, Any] | None:
        """Long-poll: return next message for this worker, or None on timeout."""
        return await self._notifier.wait_for(
            self._inbox_key(recipient_session),
            lambda: self.pop_next_for_worker(recipient_session),
            timeout,
        )

    async def record_reply(self, message_id: str, response: str) -> bool:
        async with self._db_lock:
            now = time.time()
            cur = await self.db.execute(
                "UPDATE messages SET status=?, response=?, replied_at=? "
                "WHERE id=? AND status IN (?, ?)",
                (STATUS_REPLIED, response, now, message_id, STATUS_QUEUED, STATUS_DELIVERED),
            )
            await self.db.commit()
            updated = cur.rowcount
            await cur.close()
        if updated:
            self._notifier.notify(self._outbox_key(message_id))
            return True
        return False

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,))
        row = await cur.fetchone()
        await cur.close()
        return _row_to_message(row) if row else None

    async def wait_for_reply(self, message_id: str, timeout: float) -> dict[str, Any] | None:
        async def check() -> dict[str, Any] | None:
            msg = await self.get_message(message_id)
            if msg is None:
                return None
            if msg["status"] in (STATUS_REPLIED, STATUS_CANCELLED, STATUS_EXPIRED):
                return msg
            return None

        return await self._notifier.wait_for(
            self._outbox_key(message_id),
            check,
            timeout,
        )

    async def cancel_message(self, message_id: str) -> bool:
        async with self._db_lock:
            cur = await self.db.execute(
                "UPDATE messages SET status=? WHERE id=? AND status IN (?, ?)",
                (STATUS_CANCELLED, message_id, STATUS_QUEUED, STATUS_DELIVERED),
            )
            await self.db.commit()
            updated = cur.rowcount
            await cur.close()
        if updated:
            self._notifier.notify(self._outbox_key(message_id))
            return True
        return False

    async def list_messages(
        self,
        status: str | None = None,
        limit: int = 50,
        recipient_session: str | None = None,
    ) -> list[dict[str, Any]]:
        # Optional recipient_session filter is index-backed alongside status
        # (idx_messages on recipient_session, status, created_at) so a per-session
        # inbox query stays exact even when the global queue exceeds the limit window.
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if recipient_session is not None:
            clauses.append("recipient_session=?")
            params.append(recipient_session)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cur = await self.db.execute(
            f"SELECT * FROM messages{where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_message(r) for r in rows]

    # ---------------------------------------------------------------- interrupts

    async def request_interrupt(self, session_id: str) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO interrupts (session_id, requested_at) VALUES (?, ?)",
            (session_id, time.time()),
        )
        await self.db.commit()

    async def consume_interrupt(self, session_id: str) -> bool:
        async with self._db_lock:
            cur = await self.db.execute(
                "DELETE FROM interrupts WHERE session_id=?",
                (session_id,),
            )
            await self.db.commit()
            had_one = cur.rowcount > 0
            await cur.close()
        return had_one

    # ------------------------------------------------------------------ approvals

    async def create_approval(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> str:
        approval_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO approvals (id, session_id, tool_name, tool_input, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (approval_id, session_id, tool_name, json.dumps(tool_input), time.time()),
        )
        await self.db.commit()
        self._notifier.notify(self._approval_queue_key())
        return approval_id

    async def decide_approval(self, approval_id: str, decision: str, reason: str | None) -> bool:
        if decision not in (DECISION_ALLOW, DECISION_DENY):
            raise ValueError(f"decision must be 'allow' or 'deny', got {decision!r}")
        async with self._db_lock:
            cur = await self.db.execute(
                "UPDATE approvals SET decision=?, reason=?, decided_at=? "
                "WHERE id=? AND decision IS NULL",
                (decision, reason, time.time(), approval_id),
            )
            await self.db.commit()
            updated = cur.rowcount
            await cur.close()
        if updated:
            self._notifier.notify(self._approval_key(approval_id))
            return True
        return False

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,))
        row = await cur.fetchone()
        await cur.close()
        return _row_to_approval(row) if row else None

    async def wait_for_approval_decision(
        self, approval_id: str, timeout: float
    ) -> dict[str, Any] | None:
        async def check() -> dict[str, Any] | None:
            a = await self.get_approval(approval_id)
            if a is None or a["decision"] is None:
                return None
            return a

        return await self._notifier.wait_for(
            self._approval_key(approval_id),
            check,
            timeout,
        )

    async def list_pending_approvals(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM approvals WHERE decision IS NULL ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_approval(r) for r in rows]

    # -------------------------------------------------------------- teams outbox
    # ADR-0013: a peer live session asks the hub to post to Teams. Mirrors approvals
    # (create -> controller drains pending -> controller completes -> requester awaits
    # the result), but on its OWN table so it never touches the approval path.

    async def create_teams_send(
        self,
        requester: str,
        text: str,
        target: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        request_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO teams_outbox "
            "(id, requester, target, text, metadata, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                requester,
                target,
                text,
                json.dumps(metadata) if metadata else None,
                OUTBOX_PENDING,
                time.time(),
            ),
        )
        await self.db.commit()
        self._notifier.notify(self._teams_outbox_queue_key())
        return request_id

    async def list_pending_teams_sends(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM teams_outbox WHERE status=? ORDER BY created_at ASC LIMIT ?",
            (OUTBOX_PENDING, limit),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_teams_send(r) for r in rows]

    async def wait_for_pending_teams_sends(
        self, timeout: float, limit: int = 50
    ) -> list[dict[str, Any]]:
        async def check() -> list[dict[str, Any]] | None:
            rows = await self.list_pending_teams_sends(limit)
            return rows if rows else None

        result = await self._notifier.wait_for(
            self._teams_outbox_queue_key(), check, timeout
        )
        return result or []

    async def complete_teams_send(
        self, request_id: str, ok: bool, detail: str | None = None
    ) -> bool:
        async with self._db_lock:
            cur = await self.db.execute(
                "UPDATE teams_outbox SET status=?, ok=?, detail=?, completed_at=? "
                "WHERE id=? AND status=?",
                (OUTBOX_DONE, 1 if ok else 0, detail, time.time(), request_id, OUTBOX_PENDING),
            )
            await self.db.commit()
            updated = cur.rowcount
            await cur.close()
        if updated:
            self._notifier.notify(self._teams_outbox_key(request_id))
            return True
        return False

    async def get_teams_send(self, request_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM teams_outbox WHERE id=?", (request_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return _row_to_teams_send(row) if row else None

    async def wait_for_teams_send_result(
        self, request_id: str, timeout: float
    ) -> dict[str, Any] | None:
        async def check() -> dict[str, Any] | None:
            r = await self.get_teams_send(request_id)
            if r is None or r["status"] != OUTBOX_DONE:
                return None
            return r

        return await self._notifier.wait_for(
            self._teams_outbox_key(request_id), check, timeout
        )

    # --------------------------------------------------------------- session relay
    # Session-to-session messaging: a peer live session asks the hub (the only node that
    # spans all peers) to LIST other sessions or SEND a message to one. Same create -> hub
    # drains pending -> hub completes -> requester awaits shape as teams_outbox, on its OWN
    # table so a bug here cannot touch the approval / teams / worker-message paths. The hub
    # (brain SessionRelayWatcher) does the privileged cross-peer routing; this is just the
    # durable relay queue. `op` is 'list' or 'send'; `payload`/`result` are opaque JSON here.

    async def create_session_op(
        self,
        requester: str,
        op: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        request_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO session_relay "
            "(id, requester, op, payload, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                request_id,
                requester,
                op,
                json.dumps(payload) if payload else None,
                OUTBOX_PENDING,
                time.time(),
            ),
        )
        await self.db.commit()
        self._notifier.notify(self._session_relay_queue_key())
        return request_id

    async def list_pending_session_ops(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM session_relay WHERE status=? ORDER BY created_at ASC LIMIT ?",
            (OUTBOX_PENDING, limit),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_session_op(r) for r in rows]

    async def wait_for_pending_session_ops(
        self, timeout: float, limit: int = 50
    ) -> list[dict[str, Any]]:
        async def check() -> list[dict[str, Any]] | None:
            rows = await self.list_pending_session_ops(limit)
            return rows if rows else None

        result = await self._notifier.wait_for(
            self._session_relay_queue_key(), check, timeout
        )
        return result or []

    async def complete_session_op(
        self, request_id: str, ok: bool, result: dict[str, Any] | None = None
    ) -> bool:
        async with self._db_lock:
            cur = await self.db.execute(
                "UPDATE session_relay SET status=?, ok=?, result=?, completed_at=? "
                "WHERE id=? AND status=?",
                (
                    OUTBOX_DONE,
                    1 if ok else 0,
                    json.dumps(result) if result is not None else None,
                    time.time(),
                    request_id,
                    OUTBOX_PENDING,
                ),
            )
            await self.db.commit()
            updated = cur.rowcount
            await cur.close()
        if updated:
            self._notifier.notify(self._session_relay_key(request_id))
            return True
        return False

    async def get_session_op(self, request_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM session_relay WHERE id=?", (request_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return _row_to_session_op(row) if row else None

    async def wait_for_session_op_result(
        self, request_id: str, timeout: float
    ) -> dict[str, Any] | None:
        async def check() -> dict[str, Any] | None:
            r = await self.get_session_op(request_id)
            if r is None or r["status"] != OUTBOX_DONE:
                return None
            return r

        return await self._notifier.wait_for(
            self._session_relay_key(request_id), check, timeout
        )

    # ------------------------------------------------------------------- pub/sub

    async def publish(self, channel: str, sender: str, payload: dict[str, Any]) -> int:
        cur = await self.db.execute(
            "INSERT INTO pubsub (channel, sender, payload, created_at) VALUES (?, ?, ?, ?)",
            (channel, sender, json.dumps(payload), time.time()),
        )
        await self.db.commit()
        new_id = cur.lastrowid
        await cur.close()
        self._notifier.notify(self._pubsub_key(channel))
        return new_id or 0

    async def read_pubsub_after(
        self, channel: str, after_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM pubsub WHERE channel=? AND id>? ORDER BY id ASC LIMIT ?",
            (channel, after_id, limit),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_pubsub(r) for r in rows]

    async def wait_for_pubsub(
        self, channel: str, after_id: int, timeout: float
    ) -> list[dict[str, Any]]:
        async def check() -> list[dict[str, Any]] | None:
            msgs = await self.read_pubsub_after(channel, after_id)
            return msgs if msgs else None

        result = await self._notifier.wait_for(
            self._pubsub_key(channel),
            check,
            timeout,
        )
        return result or []

    # ------------------------------------------------------------------ presence

    async def announce(
        self,
        identity: str,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upsert a peer's presence row (identity + what it's doing + heartbeat).

        Owner-token identity guard (ECA-71 / ADR-0029). A `fast-mcp-claude-channel` sidecar is
        the *sole* announcer for its identity; a second live process reusing the same identity
        (a claude.ai background fork of the TUI session) used to clobber the row and both would
        then claim the same mailbox — misrouting or black-holing messages. So if a row already
        exists for this identity with a **different** `metadata.announce_token` whose heartbeat
        is still **fresh** (within the who() stale window, `poll_heartbeat_s*3`), we REFUSE the
        second announcer: `{success: False, error: {code: "IDENTITY_LIVE_ELSEWHERE"}}`. A
        missing, matching, or stale token is accepted — so crash-and-relaunch and legitimate
        takeover (a dead announcer's token goes stale and is freely reclaimed) still work, and
        a pre-ECA-71 announcer that sends no token is never refused (fully backward compatible).

        The read-check-upsert runs under `_db_lock` so two concurrent announces can't both pass
        the freshness check and race the upsert (same single-process mutex the claim path uses).
        """
        incoming_token = metadata.get("announce_token") if isinstance(metadata, dict) else None
        now = time.time()
        async with self._db_lock:
            if incoming_token is not None:
                cur = await self.db.execute(
                    "SELECT metadata, updated_at FROM presence WHERE identity=?",
                    (identity,),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is not None:
                    existing_meta = json.loads(row["metadata"]) if row["metadata"] else {}
                    existing_token = (
                        existing_meta.get("announce_token")
                        if isinstance(existing_meta, dict)
                        else None
                    )
                    fresh_window = float(self.settings.poll_heartbeat_s * 3)
                    age = now - row["updated_at"]
                    if (
                        existing_token is not None
                        and existing_token != incoming_token
                        and age <= fresh_window
                    ):
                        return {
                            "success": False,
                            "error": {
                                "code": "IDENTITY_LIVE_ELSEWHERE",
                                "message": (
                                    f"identity {identity!r} is already announced by another "
                                    f"live process (owner token differs; last heartbeat "
                                    f"{age:.0f}s ago, within the {fresh_window:.0f}s freshness "
                                    f"window). Refusing to clobber it."
                                ),
                            },
                        }
            await self.db.execute(
                "INSERT INTO presence (identity, summary, metadata, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(identity) DO UPDATE SET "
                "summary=excluded.summary, metadata=excluded.metadata, "
                "updated_at=excluded.updated_at",
                (
                    identity,
                    summary,
                    json.dumps(metadata) if metadata else None,
                    now,
                ),
            )
            await self.db.commit()
        self._notifier.notify(self._presence_key())
        return {"success": True}

    async def list_presence(self, stale_after: float | None = None) -> list[dict[str, Any]]:
        """Return known peers, freshest first. If stale_after is set, drop rows
        whose last heartbeat is older than that many seconds."""
        cur = await self.db.execute("SELECT * FROM presence ORDER BY updated_at DESC")
        rows = await cur.fetchall()
        await cur.close()
        now = time.time()
        out: list[dict[str, Any]] = []
        for r in rows:
            if stale_after is not None and (now - r["updated_at"]) > stale_after:
                continue
            out.append(_row_to_presence(r, now))
        return out

    async def forget_presence(self, identity: str) -> None:
        """Drop a peer's presence row (called on adapter shutdown)."""
        await self.db.execute("DELETE FROM presence WHERE identity=?", (identity,))
        await self.db.commit()
        self._notifier.notify(self._presence_key())

    # ------------------------------------------------------------- notifier keys

    @staticmethod
    def _inbox_key(session: str | None) -> str:
        return f"inbox:{session or '*'}"

    @staticmethod
    def _outbox_key(message_id: str) -> str:
        return f"outbox:{message_id}"

    @staticmethod
    def _approval_key(approval_id: str) -> str:
        return f"approval:{approval_id}"

    @staticmethod
    def _approval_queue_key() -> str:
        return "approvals:any"

    @staticmethod
    def _teams_outbox_key(request_id: str) -> str:
        return f"teams_outbox:{request_id}"

    @staticmethod
    def _teams_outbox_queue_key() -> str:
        return "teams_outbox:any"

    @staticmethod
    def _session_relay_key(request_id: str) -> str:
        return f"session_relay:{request_id}"

    @staticmethod
    def _session_relay_queue_key() -> str:
        return "session_relay:any"

    @staticmethod
    def _pubsub_key(channel: str) -> str:
        return f"pubsub:{channel}"

    @staticmethod
    def _presence_key() -> str:
        return "presence:any"

    # ------------------------------------------------------------------- cleanup

    async def _cleanup_once(self, cutoff: float) -> None:
        """One sweep: expire stale in-flight rows (so waiters unblock) and prune resolved
        rows older than `cutoff`. Extracted from the periodic loop so it is directly testable."""
        async with self._db_lock:
            # Mark old queued/delivered as expired so wait_for_completion unblocks
            await self.db.execute(
                "UPDATE messages SET status=? WHERE status IN (?, ?) AND created_at<?",
                (STATUS_EXPIRED, STATUS_QUEUED, STATUS_DELIVERED, cutoff),
            )
            # Delete fully-resolved old rows
            await self.db.execute(
                "DELETE FROM messages WHERE status IN (?, ?, ?) AND created_at<?",
                (STATUS_REPLIED, STATUS_CANCELLED, STATUS_EXPIRED, cutoff),
            )
            await self.db.execute(
                "DELETE FROM approvals WHERE decision IS NOT NULL AND created_at<?",
                (cutoff,),
            )
            # Expire stale pending teams-sends (so they don't dangle in the pending set),
            # then delete completed ones. Same mark-then-delete sweep as messages above; at the
            # 7-day TTL no awaiter is still waiting, so this is hygiene, not a wake path.
            await self.db.execute(
                "UPDATE teams_outbox SET status=?, ok=0, detail=? "
                "WHERE status=? AND created_at<?",
                (OUTBOX_DONE, "expired: no hub pickup in time", OUTBOX_PENDING, cutoff),
            )
            await self.db.execute(
                "DELETE FROM teams_outbox WHERE status=? AND created_at<?",
                (OUTBOX_DONE, cutoff),
            )
            # Same mark-then-delete hygiene for the session-relay queue.
            await self.db.execute(
                "UPDATE session_relay SET status=?, ok=0, result=? "
                "WHERE status=? AND created_at<?",
                (
                    OUTBOX_DONE,
                    '{"error": "expired: no hub pickup in time"}',
                    OUTBOX_PENDING,
                    cutoff,
                ),
            )
            await self.db.execute(
                "DELETE FROM session_relay WHERE status=? AND created_at<?",
                (OUTBOX_DONE, cutoff),
            )
            await self.db.execute(
                "DELETE FROM pubsub WHERE created_at<?",
                (cutoff,),
            )
            # Backstop only — `who` filters freshness via stale_after; a
            # live adapter re-announces on every heartbeat.
            await self.db.execute(
                "DELETE FROM presence WHERE updated_at<?",
                (cutoff,),
            )
            await self.db.commit()

    async def _periodic_cleanup(self) -> None:
        """Background task — expires old queued messages and prunes ancient rows."""
        ttl = self.settings.store_ttl_seconds
        while True:
            try:
                await asyncio.sleep(min(3600, ttl // 4 or 60))
                await self._cleanup_once(time.time() - ttl)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Periodic cleanup failed")


# ---------------------------------------------------------------- row converters


def _row_to_message(row: aiosqlite.Row, delivered_at: float | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "sender": row["sender"],
        "recipient_session": row["recipient_session"],
        "prompt": row["prompt"],
        "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
        "status": row["status"],
        "response": row["response"],
        "created_at": row["created_at"],
        "delivered_at": delivered_at if delivered_at is not None else row["delivered_at"],
        "replied_at": row["replied_at"],
    }


def _row_to_approval(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "tool_name": row["tool_name"],
        "tool_input": json.loads(row["tool_input"]),
        "decision": row["decision"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


def _row_to_teams_send(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "requester": row["requester"],
        "target": row["target"],
        "text": row["text"],
        "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
        "status": row["status"],
        "ok": (None if row["ok"] is None else bool(row["ok"])),
        "detail": row["detail"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def _row_to_session_op(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "requester": row["requester"],
        "op": row["op"],
        "payload": json.loads(row["payload"]) if row["payload"] else None,
        "status": row["status"],
        "ok": (None if row["ok"] is None else bool(row["ok"])),
        "result": json.loads(row["result"]) if row["result"] else None,
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def _row_to_pubsub(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "channel": row["channel"],
        "sender": row["sender"],
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"],
    }


def _row_to_presence(row: aiosqlite.Row, now: float) -> dict[str, Any]:
    return {
        "identity": row["identity"],
        "summary": row["summary"],
        "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
        "updated_at": row["updated_at"],
        "age_seconds": round(now - row["updated_at"], 1),
    }


__all__ = [
    "Store",
    "Notifier",
    "STATUS_QUEUED",
    "STATUS_DELIVERED",
    "STATUS_REPLIED",
    "STATUS_CANCELLED",
    "STATUS_EXPIRED",
    "DECISION_ALLOW",
    "DECISION_DENY",
]


# Silence unused-import linter for Path
_ = Path
