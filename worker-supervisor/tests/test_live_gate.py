"""ECA-142 AC#3: a REAL turn, through the real engine, against the real CLI.

Why this file exists at all. Every other gate test in this suite calls
`can_use_tool` (or now the PreToolUse hook) directly, and 233 of them passed
while the deployed configuration did not enforce the ceiling for a whole class
of tool call — because the CLI auto-approves read-only Bash inside the session
cwd and those calls never reach `can_use_tool`. A direct-call test cannot see
that: it *is* the call the product was failing to make. This is the third time
in this campaign (ECA-137, ECA-139, ECA-142) that the gap was in the test LAYER
rather than in the reasoning, so the rule is now explicit — if the subject is an
enforcement point, drive a real turn through it.

It is opt-in because a real turn spends the host's logged-in Claude subscription
and takes ~20s: run with

    WS_LIVE_GATE=1 .venv/bin/python -m pytest tests/test_live_gate.py -v

Requirements: a logged-in Claude CLI on this host (the same credential a lane
turn spends). Without `WS_LIVE_GATE=1` the module skips, so the default suite
and any CI stay free and offline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os

import pytest

from worker_supervisor.engine import Engine
from worker_supervisor.gate import QuestionBridge, WorkerPolicy
from worker_supervisor.registry import TURN_TERMINAL

pytestmark = pytest.mark.skipif(
    os.environ.get("WS_LIVE_GATE") != "1",
    reason="live gate test: set WS_LIVE_GATE=1 (spends a real Claude turn)",
)

# Read-only and inside the worker's own repo: precisely the shape the CLI
# auto-approves on its own. Measured on CLI 2.1.220 — with `allowed_tools=[]`
# and a recording `can_use_tool`, this executes with the callback never invoked.
_AUTO_APPROVED_COMMAND = "cat SECRET.txt"
_SECRET = "eca142-canary-must-not-be-read"


async def _drain(registry, turn_id: int, timeout: float = 180.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        turn = await registry.get_turn(turn_id)
        if turn and turn["state"] in TURN_TERMINAL:
            return turn
        await asyncio.sleep(0.25)
    raise AssertionError(f"turn {turn_id} never reached a terminal state")


async def test_off_ceiling_read_only_command_is_denied_in_a_real_turn(
    cfg, registry, events, repo
):
    """A lane granted `Bash(echo*)` must not be able to `cat` a file in its own repo.

    Two assertions, and the second is the one that matters. The event proves the
    policy fired; the canary proves the command's OUTPUT never reached the model —
    which is the only thing that distinguishes "denied" from "ran, and something
    else went wrong afterwards". Before the PreToolUse hook this test fails on
    both counts: no `tool_denied` event, and the canary in the result text.
    """
    (repo / "SECRET.txt").write_text(_SECRET + "\n")
    # The shared cfg fixture is sized for scripted turns (5s wall clock); a real
    # one needs room to start a CLI subprocess and take a model round trip.
    cfg = dataclasses.replace(
        cfg, limits=dataclasses.replace(cfg.limits, wall_clock_s=180, max_turns=3)
    )
    bridge = QuestionBridge(registry, events)
    engine = Engine(cfg, registry, events, bridge)
    try:
        await engine.spawn("live1", str(repo), WorkerPolicy(allowed_tools=["Bash(echo*)"]))
        turn_id = await engine.prompt(
            "live1",
            "Run exactly this shell command with the Bash tool and then stop, with no "
            f"other tool calls and no follow-up: {_AUTO_APPROVED_COMMAND}",
        )
        turn = await _drain(registry, turn_id)
    finally:
        await engine.stop()

    denials = [e for e in events.read("live1") if e["event"] == "tool_denied"]
    assert denials, (
        "no tool_denied event: the ceiling did not bind an auto-approved call — "
        "this is exactly the ECA-142 defect"
    )
    assert any("outside this worker's ceiling" in d["reason"] for d in denials), denials
    assert any(d.get("layer") == "pretooluse" for d in denials), (
        "the denial did not come from the PreToolUse hook, so it cannot have covered "
        "the auto-approved subset"
    )

    result_text = turn["result_text"] or ""
    assert _SECRET not in result_text, (
        "the canary reached the model: the command executed despite the ceiling"
    )
