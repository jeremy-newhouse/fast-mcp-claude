"""Unit tests for the job-dir relay: envelope mapping, truncation policy, event tailing."""

from __future__ import annotations

import asyncio
import json

from spawner.launcher import build_request
from spawner.relay import (
    EventTailer,
    build_result_envelope,
    encode_capped,
    read_result,
    synthetic_error_envelope,
)


def test_build_request_pure_reasoning_no_clone():
    payload = {
        "job_id": "j1",
        "prompt": "hi",
        "limits": {"wall_clock": 1800, "max_turns": 50, "max_budget_usd": 10.0},
    }
    req = build_request(payload)
    assert req["job_id"] == "j1"
    assert req["prompt"] == "hi"
    assert "repo" not in req  # no owner/repo -> no clone
    # dispatch wall_clock -> runner wall_clock_s
    assert req["limits"] == {"wall_clock_s": 1800, "max_turns": 50, "max_budget_usd": 10.0}


def test_build_request_clones_from_owner_repo_slug():
    req = build_request({"job_id": "j2", "prompt": "p", "owner/repo": "octocat/Hello-World"})
    assert req["repo"] == {"url": "https://github.com/octocat/Hello-World.git", "clone": True}


def test_build_request_structured_repo_defaults_clone():
    req = build_request(
        {"job_id": "j3", "prompt": "p", "repo": {"url": "https://x/y.git", "ref": "main"}}
    )
    assert req["repo"]["clone"] is True
    assert req["repo"]["ref"] == "main"


def test_build_request_clones_from_bare_repo_string():
    # The REAL ECA-66 dispatch field: payload["repo"] is a bare "<owner>/<name>" string
    # (nats_dispatch.py dev@5fb91dc). It must resolve to a clone block, not fall through to None.
    req = build_request({"job_id": "j4", "prompt": "p", "repo": "octocat/Hello-World"})
    assert req["repo"] == {"url": "https://github.com/octocat/Hello-World.git", "clone": True}


def test_build_request_bare_repo_string_carries_ref():
    req = build_request({"job_id": "j5", "prompt": "p", "repo": "octocat/spoon", "ref": "dev"})
    assert req["repo"] == {
        "url": "https://github.com/octocat/spoon.git", "clone": True, "ref": "dev"
    }


def test_build_request_malformed_repo_string_ignored():
    # Malformed slugs (no slash, too many slashes, whitespace, empty half) resolve to no clone —
    # the job runs repo-less rather than constructing a bad clone URL.
    for bad in ["not-a-slug", "a/b/c", "owner/", "/name", "own er/name", ""]:
        req = build_request({"job_id": "j6", "prompt": "p", "repo": bad})
        assert "repo" not in req, f"expected {bad!r} to be ignored"


def test_build_request_dict_repo_takes_precedence_over_string_shape():
    # A dict is still honored as a structured repo block (not treated as a string).
    req = build_request({"job_id": "j7", "prompt": "p", "repo": {"url": "https://x/y.git"}})
    assert req["repo"] == {"url": "https://x/y.git", "clone": True}


def test_result_envelope_completed_is_ok():
    env = build_result_envelope(
        {"job_id": "j", "state": "completed", "final_text": "answer",
         "total_cost_usd": 0.42, "usage": {"input": 1}, "num_turns": 3}
    )
    assert env["ok"] is True
    assert env["text"] == "answer"
    assert env["total_cost_usd"] == 0.42
    assert env["usage"] == {"input": 1}
    assert "error" not in env


def test_result_envelope_timeout_is_not_ok():
    env = build_result_envelope({"job_id": "j", "state": "timeout", "error": "wall clock"})
    assert env["ok"] is False
    assert env["error"] == "wall clock"
    assert env["state"] == "timeout"


def test_synthetic_error_envelope():
    env = synthetic_error_envelope("j", "container vanished")
    assert env["ok"] is False
    assert env["error"] == "container vanished"


def test_encode_capped_under_cap_is_verbatim():
    obj = {"ok": True, "text": "short"}
    data = encode_capped(obj, 10_000)
    assert json.loads(data) == obj


def test_encode_capped_truncates_text_with_marker():
    big = "x" * 5000
    obj = {"ok": True, "text": big, "state": "completed"}
    data = encode_capped(obj, 1000)
    assert len(data) <= 1000
    decoded = json.loads(data)
    assert decoded["truncated"] is True
    assert "truncated by spawner" in decoded["text"]
    assert decoded["text"].startswith("x")  # kept a prefix of the real body


def test_read_result_absent_returns_none(tmp_path):
    assert read_result(tmp_path) is None


def test_read_result_reads_frame(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"state": "completed"}))
    assert read_result(tmp_path)["state"] == "completed"


class _FakePub:
    def __init__(self):
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, data))


async def test_event_tailer_publishes_new_lines_only(tmp_path):
    pub = _FakePub()
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"type": "boot"}) + "\n")
    tailer = EventTailer(tmp_path, pub, "jobs.operator.j.event", 10_000)
    assert await tailer.poll_once() == 1
    # No new lines -> nothing republished.
    assert await tailer.poll_once() == 0
    with open(events, "a") as fh:
        fh.write(json.dumps({"type": "turn"}) + "\n")
    assert await tailer.poll_once() == 1
    assert len(pub.published) == 2
    assert pub.published[0][0] == "jobs.operator.j.event"
    assert json.loads(pub.published[1][1])["type"] == "turn"


async def test_event_tailer_run_until_drains_on_stop(tmp_path):
    pub = _FakePub()
    events = tmp_path / "events.jsonl"
    events.write_text("")
    tailer = EventTailer(tmp_path, pub, "s", 10_000)
    stop = asyncio.Event()

    async def writer():
        await asyncio.sleep(0.05)
        with open(events, "a") as fh:
            fh.write(json.dumps({"type": "late"}) + "\n")
        stop.set()

    await asyncio.gather(tailer.run_until(stop, interval=0.02), writer())
    assert any(json.loads(d)["type"] == "late" for _, d in pub.published)
