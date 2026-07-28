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

from .names import require_safe_worker_name

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
    """Write one capsule file; returns its path.

    Raises ValueError for an unsafe `worker` (ECA-137) and OSError for a filesystem
    failure. Neither is fatal to a turn: callers treat capsule failure as non-fatal (the
    registry row still records why the turn failed). The summary line here used to read
    "never raises past logging needs", which stopped being true the moment the name was
    validated — corrected rather than left to mislead the next reader into removing a
    caller's handler.

    ECA-137: the filename is derived from `worker` (and from `turn['id']`, which is an
    INTEGER primary key on every path and so is not attacker-controlled), so the name is
    validated FIRST, before any directory is created or any file opened. Raising is the
    right refusal here, unlike `EventLog.emit` which must stay silent: the only caller,
    `Engine._finish_failure_capsule`, already catches everything from this function and
    reports it as a `failure_capsule_error` event — whose key `EventLog` sanitises in
    turn, so the refusal cannot escape either (the ECA-135 trap: a traversal-unlink
    traded for a traversal-write).

    THIS GUARD IS DEFENCE IN DEPTH, NOT THE THING THAT STOPS THE ESCAPE IN PRODUCTION,
    and the distinction was a review finding worth keeping. `_finish_failure_capsule`
    passes `events_tail=self._events.read(name, ...)` as an ARGUMENT, and Python evaluates
    arguments before entering the call — so for a hostile name `EventLog.read` raises
    first and this function is never reached at all. Verified by instrumentation: it is
    entered for a legal lane and never for `../../escaped`; deleting this guard leaves the
    engine-level test green, because the events guard is carrying that assertion. Keep it
    anyway — it is what protects a DIRECT caller, a future caller that does not read the
    event tail first, and a reordering of that argument list. But do not read the engine
    test's name as proof that this line fires: the operator-visible error on that path
    says "invalid event log key", not "invalid worker name".

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
    require_safe_worker_name(worker)  # before mkdir: refuse without side effects
    capsules_dir.mkdir(parents=True, exist_ok=True)
    # Explicit: umask-masked, and not applied at all if the directory exists. Not
    # through a symlink though — Path.chmod follows one, and hardening.harden_home
    # deliberately refuses to chmod a link's target; these two must not disagree.
    if not capsules_dir.is_symlink():
        capsules_dir.chmod(0o700)
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
