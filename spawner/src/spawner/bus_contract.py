"""Vendored copy of the eCA coordination-bus contract constants (ECA-65, Q2 decision).

SOURCE OF TRUTH: ``evolv-coder-agent/src/evolv_coder_agent/bus.py`` (ECA-63). These are the
stream / consumer / subject declarations the hub provisions with ``ensure_bus``; the spawner
is a NATS client in a *different repo* and must declare its durable consumer against the
**exact** names + policy the hub uses. Rather than take a packaging dependency across repos,
the orchestrator approved a small vendored copy (DEV-PLAN-ECA-65 Open-Q2) — WITH this
source-of-truth header and a live ``ensure_bus`` integration test as the drift backstop
(``tests/test_integration_nats.py``): if the hub ever changes a name here, that round-trip
test fails against a real nats-server.

DO NOT add behavior here — this file is declaration constants ONLY, kept byte-compatible with
the values in ``bus.py``. If you change a value, change it in ``bus.py`` first.
"""

from __future__ import annotations

# --- Streams (bus.py stream_configs) --------------------------------------------------------
JOBS_STREAM = "JOBS"  # WorkQueue; subjects ["dispatch.*.*"]
RESULTS_STREAM = "RESULTS"  # Limits, max_age 8d; subjects ["jobs.*.*.result", "jobs.*.*.event"]
EVENTS_STREAM = "EVENTS"  # Interest; subjects ["events.>"]

# --- JOBS consumer shape (bus.py: ECA-65 owns spawner-<member>-<machine>) --------------------
JOBS_MAX_DELIVER = 3
JOBS_ACK_WAIT_S = 600  # 10 minutes; NO backoff list (a backoff list silently overrides AckWait)

# --- Server payload floor (bus.py MAX_PAYLOAD_FLOOR) ----------------------------------------
MAX_PAYLOAD = 8 * 1024 * 1024  # 8 MiB hub ``max_payload`` floor

# --- PRESENCE KV (bus.py presence_kv_config) -------------------------------------------------
PRESENCE_BUCKET = "PRESENCE"


def dispatch_subject(member: str, machine: str) -> str:
    """The JOBS subject a spawner filters on: ``dispatch.<member>.<machine>``."""
    return f"dispatch.{member}.{machine}"


def result_subject(member: str, job_id: str) -> str:
    """The terminal RESULTS subject the spawner publishes: ``jobs.<member>.<job_id>.result``.

    DERIVED from <member>+job_id — the backend is authoritative for the shape (there is no
    ``reply`` field in the dispatch payload; ``nats_dispatch.parse_result_subject`` is the
    inverse of this).
    """
    return f"jobs.{member}.{job_id}.result"


def event_subject(member: str, job_id: str) -> str:
    """The progress RESULTS subject: ``jobs.<member>.<job_id>.event``."""
    return f"jobs.{member}.{job_id}.event"


def presence_key(member: str, machine: str) -> str:
    """PRESENCE KV key for this spawner's heartbeat: ``presence.<member>.<machine>``."""
    return f"presence.{member}.{machine}"


def durable_name(member: str, machine: str) -> str:
    """The durable pull-consumer name this spawner owns: ``spawner-<member>-<machine>``."""
    return f"spawner-{member}-{machine}"


def job_event_subject(member: str, machine: str, state: str) -> str:
    """Raw fleet event on the EVENTS stream: ``events.<member>.job.<state>``.

    The hub re-publisher scrubs these to ``events.team.*`` — not the spawner's concern.
    """
    return f"events.{member}.job.{state}"
