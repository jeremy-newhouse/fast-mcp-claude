"""Failure capsules (Amendment A6): a failed turn leaves a self-contained,
portable evidence bundle beside the worker log — prompt, options snapshot,
last-N events, stderr tail, session id + resume chain. Autonomous work must be
reviewable, not ephemeral. The failed epoch is kept, never auto-cycled over.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LAST_N_EVENTS = 50


def write_capsule(
    capsules_dir: Path,
    *,
    worker: str,
    turn: dict[str, Any],
    reason: str,
    options_snapshot: dict[str, Any],
    events_tail: list[dict[str, Any]],
    stderr_tail: list[str],
    resume_chain: list[str | None],
) -> Path:
    """Write one capsule file; returns its path. Never raises past logging needs —
    callers treat capsule failure as non-fatal (the registry row still records why).

    ECA-136: a capsule carries the turn's prompt and its raw subprocess stderr, so
    it is written 0600 in a 0700 directory rather than at the umask's 0644/0755.
    O_EXCL is deliberate, for the reason ECA-135 chose it: this filename is
    PREDICTABLE (worker, turn id, and a timestamp at one-second granularity), so a
    pre-planted hard link would otherwise receive the whole payload. The cost is a
    behaviour change — two capsules for one turn inside the same second now raise
    FileExistsError instead of the second silently overwriting the first — which the
    caller already handles: Engine._finish_failure_capsule catches and emits
    `failure_capsule_error`.
    """
    capsules_dir.mkdir(parents=True, exist_ok=True)
    capsules_dir.chmod(0o700)  # explicit: umask-masked, and skipped if it exists
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = capsules_dir / f"{worker}-turn{turn.get('id')}-{ts}.json"
    payload = {
        "written_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "worker": worker,
        "reason": reason,
        "turn": turn,
        "options": options_snapshot,
        "events_tail": events_tail[-LAST_N_EVENTS:],
        "stderr_tail": stderr_tail,
        "resume_chain": resume_chain,
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)  # nothing owns it yet; don't leak the descriptor
        raise
    # fdopen owns the fd from here and closes it itself on construction failure, so
    # it must stay OUTSIDE the handler above (a double close would hand back a
    # number another thread may already hold).
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")
    return path
