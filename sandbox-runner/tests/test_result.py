"""Unit tests for the job-dir relay contract (events.jsonl + atomic result.json)."""

from __future__ import annotations

import json

from sandbox_runner.result import (
    EVENTS_FILENAME,
    RESULT_FILENAME,
    JobRelay,
    JobState,
    build_result,
)


def _read_events(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_emit_appends_one_line_per_event(tmp_path):
    relay = JobRelay(tmp_path, "job-1")
    relay.emit("lifecycle", phase="boot")
    relay.emit("tool_use", tool="Read")
    events = _read_events(tmp_path / EVENTS_FILENAME)
    assert [e["type"] for e in events] == ["lifecycle", "tool_use"]
    assert all(e["job_id"] == "job-1" and "ts" in e for e in events)
    assert events[1]["tool"] == "Read"


def test_finalize_writes_result_atomically_last(tmp_path):
    relay = JobRelay(tmp_path, "job-2")
    result = build_result(
        state=JobState.COMPLETED,
        total_cost_usd=0.42,
        num_turns=3,
        usage={"input_tokens": 10},
        final_text="done",
        started_at="2026-07-06T00:00:00.000+00:00",
        duration_ms=1234,
    )
    out = relay.finalize(result)
    assert out.name == RESULT_FILENAME
    # No leftover temp file — the atomic rename consumed it.
    assert not (tmp_path / (RESULT_FILENAME + ".tmp")).exists()
    data = json.loads(out.read_text())
    assert data["state"] == "completed"
    assert data["total_cost_usd"] == 0.42
    assert data["num_turns"] == 3
    assert data["job_id"] == "job-2"
    assert data["ended_at"] >= data["started_at"]


def test_finalize_is_idempotent(tmp_path):
    relay = JobRelay(tmp_path, "job-3")
    r1 = build_result(
        state=JobState.TIMEOUT,
        total_cost_usd=None,
        num_turns=None,
        usage=None,
        final_text=None,
        started_at="2026-07-06T00:00:00.000+00:00",
        duration_ms=0,
        error="wall clock exceeded (1s)",
    )
    relay.finalize(r1)
    first = (tmp_path / RESULT_FILENAME).read_text()
    # Second finalize (e.g. from a defensive path) must not clobber the frame.
    relay.finalize(build_result(
        state=JobState.COMPLETED, total_cost_usd=9.0, num_turns=1, usage=None,
        final_text="x", started_at="2026-07-06T00:00:00.000+00:00", duration_ms=1,
    ))
    assert (tmp_path / RESULT_FILENAME).read_text() == first


def test_emit_after_finalize_is_ignored(tmp_path):
    relay = JobRelay(tmp_path, "job-4")
    relay.emit("lifecycle", phase="boot")
    relay.finalize(build_result(
        state=JobState.COMPLETED, total_cost_usd=0.0, num_turns=0, usage=None,
        final_text=None, started_at="2026-07-06T00:00:00.000+00:00", duration_ms=0,
    ))
    relay.emit("lifecycle", phase="after")  # should be a no-op
    events = _read_events(tmp_path / EVENTS_FILENAME)
    assert [e.get("phase") for e in events] == ["boot"]


def test_all_job_states_serialize():
    for st in JobState:
        r = build_result(
            state=st, total_cost_usd=None, num_turns=None, usage=None,
            final_text=None, started_at="2026-07-06T00:00:00.000+00:00", duration_ms=0,
        )
        assert r["state"] == st.value
