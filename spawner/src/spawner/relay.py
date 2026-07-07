"""Job-dir relay + payload policy (ECA-65 AC#3/#5).

The container writes to a bind-mounted job dir (ECA-64 contract, frozen per Q4):

  * ``events.jsonl`` — one JSON object per line, appended+fsync'd live. We tail it and republish
    each line to ``jobs.<member>.<job_id>.event``.
  * ``result.json`` — the single terminal frame, written LAST via atomic rename. After the
    container exits we read it once and publish ``jobs.<member>.<job_id>.result`` carrying
    ``total_cost_usd``/usage.

Payload policy (AC#5): inline ``.result``/``.event`` bodies are capped below the 8 MB server
``max_payload``. Above the cap the Object-Store claim-check is a documented follow-up; the v0
hard floor is **truncate with an explicit marker and still publish** — a result is NEVER
silently dropped for size.

The result-envelope shape is ``{ok, text|result|output|error, …}`` so the backend's
``ResultsBackend._decode_result`` (``nats_dispatch.py``) decodes it byte-for-byte unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

EVENTS_FILENAME = "events.jsonl"
RESULT_FILENAME = "result.json"

# Runner ``result.json`` states (sandbox_runner.result.JobState). Only "completed" is ok.
_OK_STATE = "completed"

_TRUNC_MARKER = (
    "\n\n…(result truncated by spawner — exceeded inline cap; claim-check is a follow-up)"
)


class Publisher(Protocol):
    async def publish(self, subject: str, data: bytes) -> None: ...


def build_result_envelope(result_json: dict[str, Any]) -> dict[str, Any]:
    """Map the runner ``result.json`` frame onto the backend's decode envelope.

    ``_decode_result`` reads ``ok`` + first of ``text|result|output|error``; we ALSO carry
    ``total_cost_usd``/usage/state/num_turns (AC#3) — extra keys are ignored by the decoder.
    """
    state = result_json.get("state")
    ok = state == _OK_STATE
    text = result_json.get("final_text") or ""
    error = result_json.get("error")
    envelope: dict[str, Any] = {
        "ok": ok,
        "text": text,
        "state": state,
        "total_cost_usd": result_json.get("total_cost_usd"),
        "usage": result_json.get("usage"),
        "num_turns": result_json.get("num_turns"),
        "job_id": result_json.get("job_id"),
    }
    if error:
        envelope["error"] = error
    return envelope


def synthetic_error_envelope(job_id: str, error: str) -> dict[str, Any]:
    """Envelope for a spawner-detected failure (no result.json — crashed mid-run / bad exit)."""
    return {"ok": False, "text": "", "error": error, "state": "error", "job_id": job_id,
            "total_cost_usd": None, "usage": None, "num_turns": None}


def encode_capped(obj: dict[str, Any], cap: int) -> bytes:
    """Serialize ``obj`` to JSON bytes; if it exceeds ``cap``, truncate the ``text`` field with an
    explicit marker and re-serialize (AC#5 — never silently drop). Returns UTF-8 bytes."""
    data = json.dumps(obj).encode("utf-8")
    if len(data) <= cap:
        return data
    text = obj.get("text")
    shrunk = dict(obj)
    shrunk["truncated"] = True
    if not isinstance(text, str):
        # Non-text overflow (huge usage/etc.) — we can't trim the body; drop text and mark it.
        shrunk["text"] = _TRUNC_MARKER.strip()
        logger.warning("non-text payload exceeds cap %d — publishing marker frame", cap)
        return json.dumps(shrunk).encode("utf-8")

    # Estimate a byte budget for the text, then shrink until the SERIALIZED frame fits (JSON
    # escaping of the marker/body can expand length, so verify empirically and back off).
    shrunk["text"] = _TRUNC_MARKER.strip()
    overhead = len(json.dumps(shrunk).encode("utf-8"))
    budget = max(cap - overhead, 0)
    raw = text.encode("utf-8")
    while budget >= 0:
        prefix = raw[:budget].decode("utf-8", errors="ignore")
        shrunk["text"] = prefix + _TRUNC_MARKER
        encoded = json.dumps(shrunk).encode("utf-8")
        if len(encoded) <= cap or budget == 0:
            logger.warning(
                "result/event body %d bytes exceeds inline cap %d — truncated with marker",
                len(data), cap,
            )
            return encoded
        budget -= max((len(encoded) - cap), 1)
        budget = max(budget, 0)
    # Unreachable in practice; return the marker-only frame as the hard floor.
    shrunk["text"] = _TRUNC_MARKER.strip()
    return json.dumps(shrunk).encode("utf-8")


def read_result(job_dir: Path) -> dict[str, Any] | None:
    """Read ``result.json`` once (atomic-rename-last guarantees no half-write). None if absent."""
    path = job_dir / RESULT_FILENAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        logger.warning("failed reading %s: %s", path, e)
        return None


class EventTailer:
    """Tail ``events.jsonl`` and republish each new line to the ``.event`` subject.

    Poll-based (the runner emits O(turns) events, not a hot loop): reads any lines that appeared
    since the last offset. ``drain()`` flushes the tail after the container exits so no trailing
    event is lost.
    """

    def __init__(
        self, job_dir: Path, publisher: Publisher, event_subject: str, cap: int
    ):
        self._path = job_dir / EVENTS_FILENAME
        self._pub = publisher
        self._subject = event_subject
        self._cap = cap
        self._offset = 0

    async def poll_once(self) -> int:
        """Publish any complete new lines; return how many were published."""
        if not self._path.is_file():
            return 0
        try:
            with open(self._path, encoding="utf-8") as fh:
                fh.seek(self._offset)
                data = fh.read()
                self._offset = fh.tell()
        except OSError:
            return 0
        count = 0
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                frame = {"raw": line}
            await self._pub.publish(self._subject, encode_capped(frame, self._cap))
            count += 1
        return count

    async def run_until(self, stop: asyncio.Event, interval: float = 0.5) -> None:
        """Poll on an interval until ``stop`` is set, then one final drain."""
        while not stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
        await self.poll_once()  # final drain
