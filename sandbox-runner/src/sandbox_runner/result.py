"""The job-directory relay contract (AC#4, Q5-confirmed shape).

The runner never talks NATS. It writes to a bind-mounted **job directory** that
the spawner (ECA-65) tails/consumes:

  * ``events.jsonl`` — one JSON object per line, appended and fsync'd as events
    occur. The spawner tails this and republishes each line as a ``.event``.
  * ``result.json``  — the single terminal frame, written **last** via atomic
    rename (write ``result.json.tmp`` then ``os.replace``). The spawner, seeing
    the container exit, reads this once and publishes the ``.result``. The atomic
    rename guarantees the spawner never observes a half-written result.

Nothing here imports the SDK — it is pure, unit-testable I/O so the frame
contract can be validated without a live model leg.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

EVENTS_FILENAME = "events.jsonl"
RESULT_FILENAME = "result.json"
_RESULT_TMP_SUFFIX = ".tmp"


class JobState(str, Enum):
    """Terminal states carried in ``result.json``."""

    COMPLETED = "completed"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    TURN_LIMIT = "turn_limit"
    ERROR = "error"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JobRelay:
    """Append-only event writer + atomic terminal result writer for one job."""

    def __init__(self, job_dir: str | os.PathLike[str], job_id: str) -> None:
        self.job_dir = Path(job_dir)
        self.job_id = job_id
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self.job_dir / EVENTS_FILENAME
        self._result_path = self.job_dir / RESULT_FILENAME
        self._closed = False

    @property
    def events_path(self) -> Path:
        return self._events_path

    @property
    def result_path(self) -> Path:
        return self._result_path

    def emit(self, event_type: str, **fields: Any) -> None:
        """Append one event line and fsync so a tailing spawner sees it promptly."""
        if self._closed:
            return
        frame = {"ts": _utcnow_iso(), "job_id": self.job_id, "type": event_type, **fields}
        line = json.dumps(frame, ensure_ascii=False, default=str) + "\n"
        # Open-append-fsync per event: correctness (durability for the tailer)
        # over throughput — jobs emit O(turns) events, not a hot loop.
        with open(self._events_path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def finalize(self, result: dict[str, Any]) -> Path:
        """Write the terminal result frame LAST via atomic rename. Idempotent."""
        if self._closed:
            return self._result_path
        frame = {"ts": _utcnow_iso(), "job_id": self.job_id, **result}
        tmp = self._result_path.with_name(self._result_path.name + _RESULT_TMP_SUFFIX)
        data = json.dumps(frame, ensure_ascii=False, indent=2, default=str)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._result_path)  # atomic within the same directory
        # fsync the directory so the rename itself is durable before container exit.
        dir_fd = os.open(self.job_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        self._closed = True
        return self._result_path


def build_result(
    *,
    state: JobState,
    total_cost_usd: float | None,
    num_turns: int | None,
    usage: dict[str, Any] | None,
    final_text: str | None,
    started_at: str,
    duration_ms: int,
    error: str | None = None,
) -> dict[str, Any]:
    """Assemble the terminal result payload (the per-job cost/DoS raw data)."""
    return {
        "state": state.value,
        "total_cost_usd": total_cost_usd,
        "num_turns": num_turns,
        "usage": usage,
        "final_text": final_text,
        "error": error,
        "started_at": started_at,
        "ended_at": _utcnow_iso(),
        "duration_ms": duration_ms,
    }
