"""Per-worker JSONL event logs (Amendment A9): tailable, replayable evidence.

One append-only file per worker under <home>/logs/<worker>.jsonl. `events` reads
it back; `attach` follows it live. Writes are line-buffered appends — crash-safe
enough for evidence (the registry, not this log, is the recovery authority).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EventLog:
    def __init__(self, logs_dir: Path) -> None:
        self._dir = logs_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def path(self, worker: str) -> Path:
        return self._dir / f"{worker}.jsonl"

    def emit(self, worker: str, event: str, **fields: Any) -> dict[str, Any]:
        record = {"ts": _now(), "worker": worker, "event": event, **fields}
        with self.path(worker).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record

    def read(self, worker: str, limit: int | None = None) -> list[dict[str, Any]]:
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
        """Async tail -f for `attach`. Starts at end-of-file, yields new records."""
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
