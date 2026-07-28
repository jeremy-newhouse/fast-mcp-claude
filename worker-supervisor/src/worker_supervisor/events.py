"""Per-worker JSONL event logs (Amendment A9): tailable, replayable evidence.

One append-only file per worker under <home>/logs/<worker>.jsonl. `events` reads
it back; `attach` follows it live. Writes are line-buffered appends — crash-safe
enough for evidence (the registry, not this log, is the recovery authority).

ECA-137: the key names the file, so it is validated in `path()` — the single
derivation every method here funnels through, including the two READ paths, which
`server.py` exposes to any control-socket caller. The three methods then differ in
what they do with a refusal, and the differences are deliberate: see `emit`.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from .names import DAEMON_KEY, require_safe_log_key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EventLog:
    def __init__(self, logs_dir: Path) -> None:
        self._dir = logs_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        # ECA-136: explicit — mkdir's mode is umask-masked, and is not applied at
        # all when the directory already exists (which it does on a live install).
        # Skipped for a symlink, because Path.chmod FOLLOWS one: an operator who
        # relocates logs/ onto another volume would otherwise have the daemon
        # silently re-mode whatever is at the other end. `hardening.harden_home`
        # takes exactly this posture and would have reported this path as skipped;
        # the two must not disagree.
        if not self._dir.is_symlink():
            self._dir.chmod(0o700)

    def path(self, worker: str) -> Path:
        """The log file for `worker`. Raises ValueError for a key that is not a plain
        filename component (ECA-137) — every read and write here derives through this
        one method, so the guard cannot be bypassed by a new caller.
        """
        require_safe_log_key(worker)
        return self._dir / f"{worker}.jsonl"

    def emit(self, worker: str, event: str, **fields: Any) -> dict[str, Any]:
        """Append one record. Never raises for a bad key, and never drops the record.

        ECA-137. `emit` is the one method here that must not propagate the refusal.
        It is called from 28 sites in the engine, several of them INSIDE failure
        handlers, and ECA-135 already established what an escape from one of those
        costs: it kills the worker's runner loop and wedges the lane silently while it
        still answers prompts. So a refused key is RE-KEYED to the daemon key rather
        than raised or discarded — dropping the record silently would delete exactly
        the evidence that a hostile name reached a writer.

        The rejected name is kept verbatim in the record BODY, where it is JSON
        content and not a path, so the log still says which row produced this. The
        `log_key_refused` stamp is applied AFTER the `**fields` spread: a caller
        cannot pass a field of that name and mask the refusal.

        This makes the ECA-135 pattern of reporting a refusal under the daemon key
        (`Engine._purge_mcp_config`) belt-and-braces rather than load-bearing. Keep
        both: the caller's explicit key produces a clean record instead of one marked
        as re-keyed, and a guard at the writer covers callers that forget.
        """
        try:
            log_path = self.path(worker)
            refused = False
        except ValueError:
            log_path = self.path(DAEMON_KEY)
            refused = True
        record = {"ts": _now(), "worker": worker, "event": event, **fields}
        if refused:
            record["log_key_refused"] = True
        # ECA-136: a worker's event log carries the turn's raw subprocess stderr
        # (turn_mcp_diagnostics.stderr_tail), so it is not public-by-default data.
        # open("a") takes no mode, so this uses the ECA-135 idiom instead. The
        # fchmod is required rather than belt-and-braces: O_CREAT's mode is ignored
        # entirely for a file that already exists, and every log file on both live
        # hosts already exists at 0644. No O_EXCL — this is an append log.
        fd = os.open(
            log_path,  # already validated above; NOT re-derived from `worker`
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)  # nothing owns it yet; don't leak the descriptor
            raise
        # fdopen takes ownership here and closes the fd itself if construction
        # fails, so it must NOT sit inside the handler above or a failure would
        # double-close a number another thread may already have been handed.
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record

    def read(self, worker: str, limit: int | None = None) -> list[dict[str, Any]]:
        """ECA-137: unlike `emit`, this propagates a refused key. Deliberate — the two
        callers both handle it and a loud refusal beats a silent `[]` that reads as "no
        events". `server.py`'s `events` verb passes a CALLER-SUPPLIED name, so without
        the guard in `path()` this was a traversal READ off the control socket; and
        `Engine._finish_failure_capsule` calls it inside a handler that turns any
        exception into a `failure_capsule_error` event.
        """
        p = self.path(worker)
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()
        if limit is not None:
            lines = lines[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"ts": None, "worker": worker, "event": "unparseable", "raw": line})
        return out

    async def follow(self, worker: str, poll_s: float = 0.5) -> AsyncIterator[dict[str, Any]]:
        """Async tail -f for `attach`. Starts at end-of-file, yields new records.

        Propagates a refused key like `read` (ECA-137): `attach` is an interactive
        operator verb, where tailing nothing forever is worse than an error.
        """
        p = self.path(worker)
        pos = p.stat().st_size if p.exists() else 0
        while True:
            if p.exists():
                size = p.stat().st_size
                if size < pos:  # rotated/truncated
                    pos = 0
                if size > pos:
                    with p.open("r", encoding="utf-8") as f:
                        f.seek(pos)
                        chunk = f.read()
                        pos = f.tell()
                    for line in chunk.splitlines():
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            yield {"event": "unparseable", "raw": line}
            await asyncio.sleep(poll_s)
