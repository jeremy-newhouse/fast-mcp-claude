"""ECA-147 AC#1/AC#2: G7 against the REAL SDK and the REAL CLI, not a fake `query`.

Why this file exists. G7 — "a dead resume chain is never silently continued fresh:
end the epoch and ground the next one on the handover file" — was spelled `except
ProcessError` and was only ever tested through a fake `query` raising that type. That
proved the branch worked for callers who raise ProcessError; it could not prove
anything about what the SDK does, and the SDK is the only caller there is. ECA-147
measured it: which exception type arrives depends on WHERE inside the SDK the CLI's
exit is noticed, so two of the three possibilities skipped G7 entirely.

The fix keys the decision on evidence (the CLI never announced the session) instead of
on a type, and the unit tests parametrize all three types. This test is the layer
neither of those can replace: it drives a resume the real CLI actually refuses, so the
premise itself — "a rejected resume dies before the init frame" — is under test rather
than assumed.

Cost: the turn under test spends nothing — the CLI rejects the session id and exits
before contacting the API. G7's RECOVERY is a different matter: the restore turn it
enqueues is an ordinary model turn, and a `done` restore auto-enqueues a continuation
(ECA-84), so left to run this test would drive a real lane indefinitely. The recovery
leg is therefore stubbed out (see `_only_the_first_turn_is_live`) — its being ENQUEUED
is the whole assertion; what it then does belongs to other tests.

Not just about cost: with a real restore turn in flight, `Engine.stop()` was measured
hanging indefinitely on this host (560s and still going, no CLI subprocess alive,
released only by SIGINT). That is a pre-existing supervisor defect, filed separately —
but it is also why this test must not leave a live turn to cancel.

Opt-in because it needs a real `claude` binary on PATH:

    WS_LIVE_RESUME=1 .venv/bin/python -m pytest tests/test_live_resume.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import uuid

import pytest

from worker_supervisor import engine as engine_module
from worker_supervisor.engine import Engine, session_transcript_path
from worker_supervisor.gate import QuestionBridge
from worker_supervisor.registry import TURN_TERMINAL, _now as registry_now

pytestmark = pytest.mark.skipif(
    os.environ.get("WS_LIVE_RESUME") != "1",
    reason="live resume test: set WS_LIVE_RESUME=1 (needs a real claude CLI)",
)


async def _drain(registry, turn_id: int, timeout: float = 180.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        turn = await registry.get_turn(turn_id)
        if turn and turn["state"] in TURN_TERMINAL:
            return turn
        await asyncio.sleep(0.25)
    raise AssertionError(f"turn {turn_id} never reached a terminal state")


def _only_the_first_turn_is_live(monkeypatch) -> None:
    """The turn under test runs against the real SDK; every later turn does not.

    The ONE thing being measured here is what the real CLI does with a resume it
    refuses. G7's answer to that is to enqueue a restore turn, and running THAT would
    mean an unbounded live lane (a `done` restore auto-enqueues a continuation) plus a
    turn in flight for `stop()` to cancel, which currently hangs. So the second call
    onward fails instantly and pre-init — which is a shape the SDK genuinely produces,
    and lands on the ordinary ladder because a fresh epoch carries no session to resume.
    """
    real_query = engine_module.query
    seen = {"n": 0}

    def bounded_query(**kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            return real_query(**kwargs)

        async def _instant_failure():
            raise RuntimeError("recovery turn deliberately not run by this test")
            yield  # pragma: no cover — makes this an async generator

        return _instant_failure()

    monkeypatch.setattr(engine_module, "query", bounded_query)


async def _seed_finished_turn(registry, worker: str, session_id: str) -> None:
    """A previous turn in this epoch left `session_id` behind.

    Written straight to the row because the alternative is spending a real model turn
    to produce one, and the turn's CONTENT is irrelevant here — all that matters is
    that `_pick_resume_target` finds this id and hands it to the CLI. Inserted already
    terminal so the runner never claims it.
    """
    epoch = await registry.current_epoch(worker)
    # registry._now()'s format, not SQLite's `datetime('now')` — nothing parses
    # turns.created_at today, but a hand-written row that does not look like the ones
    # the code writes is a trap for whoever adds the first reader.
    now = registry_now()
    await registry.db.execute(
        "INSERT INTO turns (epoch_id, worker, kind, prompt, state, session_id,"
        " created_at, finished_at) VALUES (?, ?, 'prompt', ?, 'done', ?, ?, ?)",
        (epoch["id"], worker, "seeded by test", session_id, now, now),
    )
    await registry.db.commit()


async def test_a_resume_the_real_cli_refuses_rolls_the_epoch(
    cfg, registry, events, repo, monkeypatch
):
    """The CLI is handed a session id whose transcript exists but is not a transcript.

    That is the "present but rejected" case: `_pick_resume_target`'s `_transcript_exists`
    guard is satisfied, so the id really does reach `ClaudeAgentOptions.resume`, and the
    CLI refuses it ("No conversation found with session ID: ..."). Before the fix this
    reached G7 by accident — the raw ProcessError is re-raised to the pending
    `initialize` control request — and the same refusal arriving as a bare `Exception`
    or a `CLIConnectionError` did not. Now the trigger is the absent init frame, which
    is what this test measures on the way past.
    """
    session_id = str(uuid.uuid4())
    transcript = session_transcript_path(str(repo), session_id)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("not a transcript\n", encoding="utf-8")

    # 30s is ~20x the measured time for the CLI to refuse a resume and exit; it is
    # generous so a slow host cannot turn this into a wall-clock timeout, which would
    # be a different terminal state and a false failure.
    cfg = dataclasses.replace(
        cfg, limits=dataclasses.replace(cfg.limits, wall_clock_s=30, max_turns=1)
    )
    bridge = QuestionBridge(registry, events)
    engine = Engine(cfg, registry, events, bridge)
    _only_the_first_turn_is_live(monkeypatch)
    try:
        await engine.spawn("liveres1", str(repo))
        await _seed_finished_turn(registry, "liveres1", session_id)
        turn_id = await engine.prompt("liveres1", "say ok")
        turn = await _drain(registry, turn_id)

        # FIRST, before anything else can fail and take the blame: was the CLI actually
        # asked to resume the seeded id? If _pick_resume_target ever returns None, this
        # runs an ordinary fresh turn that SUCCEEDS — no G7, no restore, a real model
        # call — and every assertion below would misreport that as a G7 defect.
        assert turn["resume_from"] == session_id, (
            "the CLI was never asked to resume the seeded id, so this test proved nothing"
        )

        # G7's recovery: a fresh epoch, and a restore turn to ground it.
        deadline = asyncio.get_event_loop().time() + 30
        restore: list | None = None
        while not restore and asyncio.get_event_loop().time() < deadline:
            history = await registry.history("liveres1", limit=10)
            restore = [t for t in history if t["kind"] == "restore"]
            if not restore:
                await asyncio.sleep(0.02)
        assert restore, "no restore turn was enqueued, so the next epoch is ungrounded"
        epoch = await registry.current_epoch("liveres1")
        assert epoch["seq"] == 2

        assert turn["state"] == "error"
        assert "resume failed" in (turn["error"] or ""), (
            f"G7 did not fire on a resume the real CLI refused: {turn['error']!r}"
        )

        rows = events.read("liveres1")
        failed = [e for e in rows if e["event"] == "resume_failed"]
        assert failed and failed[-1]["resume"] == session_id

        # The premise of the whole fix, measured here rather than asserted: the CLI
        # refuses a resume BEFORE it announces the session, so "no init frame" really
        # does identify a chain that never came alive.
        capsule = json.loads(
            sorted(cfg.capsules_dir.glob(f"liveres1-turn{turn_id}-*.json"))[-1].read_text(
                encoding="utf-8"
            )
        )
        diag = capsule["result_diagnostics"]
        assert diag["saw_init"] is False and diag["frames"] == 0, (
            f"the CLI emitted frames for a refused resume: {diag}"
        )
        # And the CLI's own words reached the capsule (they are only ever on stderr for
        # this failure — there is no result frame to carry them).
        assert any(
            "No conversation found" in line for line in capsule["stderr_tail"]
        ), capsule["stderr_tail"]
    finally:
        await engine.stop()
        transcript.unlink(missing_ok=True)
        # The parent is this repo's own cwd-slug directory under the operator's real
        # ~/.claude/projects; remove it if this test is what created it.
        with contextlib.suppress(OSError):
            transcript.parent.rmdir()
