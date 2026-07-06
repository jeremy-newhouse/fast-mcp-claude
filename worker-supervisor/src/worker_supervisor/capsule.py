"""Failure capsules (Amendment A6): a failed turn leaves a self-contained,
portable evidence bundle beside the worker log — prompt, options snapshot,
last-N events, stderr tail, session id + resume chain. Autonomous work must be
reviewable, not ephemeral. The failed epoch is kept, never auto-cycled over.
"""

from __future__ import annotations

import json
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
    callers treat capsule failure as non-fatal (the registry row still records why)."""
    capsules_dir.mkdir(parents=True, exist_ok=True)
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
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")
    return path
