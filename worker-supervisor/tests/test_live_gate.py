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

from worker_supervisor import engine as engine_module
from worker_supervisor.engine import Engine
from worker_supervisor.gate import QuestionBridge, WorkerPolicy
from worker_supervisor.registry import TURN_TERMINAL

_real_query = engine_module.query

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


@pytest.mark.timeout(240)
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


@pytest.mark.timeout(240)
async def test_granted_prefix_cannot_smuggle_a_second_command_in_a_real_turn(
    cfg, registry, events, repo
):
    """ECA-144 AC#1, end to end: `echo hi && cat SECRET.txt` under `Bash(echo *)`.

    A different path through the CLI from the test above, which is why it earns its
    own real turn rather than trusting the unit tests. ECA-142 measured that a
    compound command does NOT get auto-approved — the CLI's classifier reads the
    whole string and escalates it — so this call reaches the policy on the normal
    route and the ceiling is what has to refuse it. On the shipped matcher the
    ceiling said ALLOW, so escalating changed nothing: the command ran.

    The canary assertion is again the load-bearing one. A `tool_denied` event only
    says something denied something; the absent secret says the `cat` never ran.
    """
    (repo / "SECRET.txt").write_text(_SECRET + "\n")
    smuggle = f"echo hi && {_AUTO_APPROVED_COMMAND}"
    cfg = dataclasses.replace(
        cfg, limits=dataclasses.replace(cfg.limits, wall_clock_s=180, max_turns=3)
    )
    bridge = QuestionBridge(registry, events)
    engine = Engine(cfg, registry, events, bridge)
    try:
        await engine.spawn("live2", str(repo), WorkerPolicy(allowed_tools=["Bash(echo *)"]))
        turn_id = await engine.prompt(
            "live2",
            "Run exactly this shell command with the Bash tool and then stop, with no "
            f"other tool calls and no follow-up: {smuggle}",
        )
        turn = await _drain(registry, turn_id)
    finally:
        await engine.stop()

    denials = [e for e in events.read("live2") if e["event"] == "tool_denied"]
    assert denials, "no tool_denied event: the compound command was not refused"
    assert any("cat SECRET.txt" in d["reason"] for d in denials), (
        f"the denial did not name the smuggled command: {denials}"
    )

    result_text = turn["result_text"] or ""
    assert _SECRET not in result_text, (
        "the canary reached the model: the smuggled command executed"
    )


async def _query_with_policy_hook_suppressed(*, prompt, options):
    """Strip the `matcher=None` PreToolUse hook before delegating to the real SDK.

    This is ECA-145's own scenario, reproduced rather than assumed: "a future SDK
    or CLI change stops dispatching PreToolUse callbacks, or drops `matcher=None`
    universality" (see `gate.make_policy_hook`'s docstring). Removing this one
    HookMatcher is the narrowest simulation of that — everything else (the real
    CLI subprocess, the real turn, the AskUserQuestion matcher, `can_use_tool`)
    stays live, so the CLI genuinely never calls the policy hook for this turn;
    nothing here calls `on_pre_tool_use` directly.
    """
    if "PreToolUse" in options.hooks:
        options.hooks["PreToolUse"] = [
            m for m in options.hooks["PreToolUse"] if m.matcher is not None
        ]
    async for msg in _real_query(prompt=prompt, options=options):
        yield msg


@pytest.mark.timeout(240)
async def test_a_suppressed_policy_hook_is_detected_in_a_real_turn(
    cfg, registry, events, repo, monkeypatch
):
    """ECA-145 AC#2: the detector must be proven against a REAL turn with hook
    dispatch suppressed, not a direct call to the hook function — a direct call
    is exactly what a hook silently not firing looks like from the outside, so a
    test that only ever calls the hook directly cannot see this failure mode by
    construction (the same test-layer gap ECA-137/139/142 hit before it).

    Two assertions, and the first is the one the other tests in this file don't
    need: with the hook suppressed there is nothing left to deny the
    auto-approved call, so the canary MUST reach the model this time — that is
    what proves the hook truly never fired, as opposed to firing and allowing.
    The second is this task's own detector: a distinct `policy_hook_gap` event,
    not `tool_denied` (nothing denied anything here), naming the shortfall.
    """
    (repo / "SECRET.txt").write_text(_SECRET + "\n")
    cfg = dataclasses.replace(
        cfg, limits=dataclasses.replace(cfg.limits, wall_clock_s=180, max_turns=3)
    )
    monkeypatch.setattr(engine_module, "query", _query_with_policy_hook_suppressed)
    bridge = QuestionBridge(registry, events)
    engine = Engine(cfg, registry, events, bridge)
    try:
        await engine.spawn("live3", str(repo), WorkerPolicy(allowed_tools=["Bash(echo*)"]))
        turn_id = await engine.prompt(
            "live3",
            "Run exactly this shell command with the Bash tool and then stop, with no "
            f"other tool calls and no follow-up: {_AUTO_APPROVED_COMMAND}",
        )
        turn = await _drain(registry, turn_id)
    finally:
        await engine.stop()

    result_text = turn["result_text"] or ""
    assert _SECRET in result_text, (
        "the canary never reached the model: something still policed the call, "
        "so the hook was not actually suppressed for this turn"
    )

    gaps = [e for e in events.read("live3") if e["event"] == "policy_hook_gap"]
    assert gaps, "no policy_hook_gap event: the suppressed dispatch went undetected"
    assert gaps[0]["hook_invocations"] == 0
    assert gaps[0]["tool_uses"] >= 1
