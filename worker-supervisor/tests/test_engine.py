"""Turn engine behavior against a scripted fake `query`: resume chaining, the
failure ladder (retry-once / resume_failed), budget refusal, cycling (manual +
auto), and lifecycle records — the code-provable halves of AC-WS-1/4/5/11."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ProcessError,
    ResultMessage,
    ToolUseBlock,
)

from worker_supervisor.engine import Engine
from worker_supervisor.gate import QuestionBridge
from worker_supervisor.registry import TURN_TERMINAL


def r(session_id: str, *, cost: float = 0.01, usage: dict | None = None,
      is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=90,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=cost,
        usage=usage or {"input_tokens": 1000, "cache_read_input_tokens": 0},
        result=f"result from {session_id}",
    )


def a(*tools: str, usage: dict | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id=f"t-{t}", name=t, input={}) for t in tools],
        model="test-model",
        usage=usage,
    )


def make_fake_query(script: list[Any], calls: list[Any]):
    async def fake_query(*, prompt, options, transport=None):
        idx = len(calls)
        calls.append(options)
        item = script[idx] if idx < len(script) else script[-1]
        if isinstance(item, Exception):
            raise item
        async for _ in prompt:  # consume the stream like the SDK does
            break
        for msg in item:
            yield msg

    return fake_query


@pytest.fixture
async def make_engine(cfg, registry, events, monkeypatch):
    engines: list[Engine] = []

    def _make(script: list[Any]):
        calls: list[Any] = []
        monkeypatch.setattr("worker_supervisor.engine.query", make_fake_query(script, calls))
        # Fake sessions never hit the real cwd-keyed store.
        monkeypatch.setattr(Engine, "_transcript_exists", lambda self, cwd, sid: True)
        bridge = QuestionBridge(registry, events)
        engine = Engine(cfg, registry, events, bridge)
        engines.append(engine)
        return engine, calls

    yield _make
    for e in engines:
        await e.stop()


async def wait_until(predicate, timeout: float = 5.0):
    async def _poll():
        while True:
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout)


async def terminal_turn(registry, turn_id: int, timeout: float = 5.0):
    async def _check():
        t = await registry.get_turn(turn_id)
        return t if t and t["state"] in TURN_TERMINAL else None

    return await wait_until(_check, timeout)


async def test_happy_turn_persists_session_and_telemetry(make_engine, registry, repo):
    engine, calls = make_engine([[a("Read", "Bash"), r("s1", cost=0.05)]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "do the thing")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "done"
    assert turn["session_id"] == "s1"
    assert turn["cost_usd"] == pytest.approx(0.05)
    assert "Read" in turn["tools"] and "Bash" in turn["tools"]
    worker = await registry.get_worker("w1")
    assert worker["status"] == "idle"
    assert calls[0].resume is None
    assert calls[0].setting_sources == ["project"]


async def test_turns_chain_via_resume(make_engine, registry, repo):
    engine, calls = make_engine([[r("s1")], [r("s2")]])
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "one")
    await terminal_turn(registry, t1)
    t2 = await engine.prompt("w1", "two")
    turn2 = await terminal_turn(registry, t2)
    assert turn2["session_id"] == "s2"
    assert calls[1].resume == "s1"  # the epoch chain


async def test_bare_exception_retries_once_then_succeeds(make_engine, registry, repo, events):
    """G2: mid-stream death is a bare Exception; rebuild + retry once, same resume."""
    engine, calls = make_engine([RuntimeError("subprocess died mid-stream"), [r("s1")]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "flaky")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "done" and turn["session_id"] == "s1"
    assert len(calls) == 2
    assert any(e["event"] == "turn_retry" for e in events.read("w1"))


async def test_second_failure_is_terminal_with_capsule(make_engine, registry, repo, cfg):
    engine, _ = make_engine([RuntimeError("boom 1"), RuntimeError("boom 2"), [r("sX")]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "doomed")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "error" and "boom 2" in turn["error"]
    capsules = list(cfg.capsules_dir.glob("w1-turn*.json"))
    assert capsules, "failure capsule missing (Amendment A6)"
    epoch = await registry.current_epoch("w1")
    assert epoch["seq"] == 1 and epoch["ended_at"] is None  # keep-on-failure


async def test_resume_failure_rolls_epoch_and_enqueues_restore(make_engine, registry, repo):
    """G7: ProcessError on a resumed chain -> epoch ends, restore turn grounds the next."""
    engine, calls = make_engine(
        [[r("s1")], ProcessError("resume rejected", exit_code=1), [r("s3")]]
    )
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "one")
    await terminal_turn(registry, t1)
    t2 = await engine.prompt("w1", "two")
    turn2 = await terminal_turn(registry, t2)
    assert turn2["state"] == "error" and "resume failed" in turn2["error"]

    async def _restore_done():
        rows = await registry.history("w1", limit=10)
        done = [t for t in rows if t["kind"] == "restore" and t["state"] == "done"]
        return done or None

    restore = (await wait_until(_restore_done))[0]
    assert restore["session_id"] == "s3"
    epoch = await registry.current_epoch("w1")
    assert epoch["seq"] == 2
    assert calls[2].resume is None  # fresh chain, grounded by the handover restore


async def test_epoch_budget_refuses_next_turn(make_engine, registry, repo):
    """AC-WS-5: a breached budget terminates/refuses with the reason recorded."""
    engine, _ = make_engine([[r("s1", cost=2.0)]])  # cap is 1.0 in test cfg
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "expensive")
    await terminal_turn(registry, t1)
    t2 = await engine.prompt("w1", "should refuse")
    turn2 = await terminal_turn(registry, t2)
    assert turn2["state"] == "budget_refused"
    assert "budget exhausted" in turn2["error"]


async def test_manual_cycle_rolls_epoch_and_restores(make_engine, registry, repo, events):
    engine, _ = make_engine([[r("s1")], [r("s2")], [r("s3")]])
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "work")
    await terminal_turn(registry, t1)
    await engine.cycle("w1")

    async def _cycled():
        epoch = await registry.current_epoch("w1")
        return epoch if epoch["seq"] == 2 else None

    await wait_until(_cycled)

    async def _restored():
        rows = await registry.history("w1", limit=10)
        return [t for t in rows if t["kind"] == "restore" and t["state"] == "done"] or None

    await wait_until(_restored)
    kinds = [t["kind"] for t in reversed(await registry.history("w1", limit=10))]
    assert kinds == ["prompt", "cycle_handover", "restore"]
    assert any(e["event"] == "epoch_cycled" for e in events.read("w1"))


async def test_resume_skips_unpersisted_session(cfg, registry, events, repo, monkeypatch):
    """CLI-2.1.165 gotcha: a reported session id may never reach disk — resume
    the newest PERSISTED id instead of erroring the whole epoch."""
    calls: list[Any] = []
    monkeypatch.setattr(
        "worker_supervisor.engine.query", make_fake_query([[r("s1")], [r("s2")], [r("s3")]], calls)
    )
    monkeypatch.setattr(
        Engine, "_transcript_exists", lambda self, cwd, sid: sid != "s2"  # s2 lost the race
    )
    from worker_supervisor.gate import QuestionBridge as QB

    engine = Engine(cfg, registry, events, QB(registry, events))
    try:
        await engine.spawn("w1", str(repo))
        for prompt in ("one", "two", "three"):
            tid = await engine.prompt("w1", prompt)
            await terminal_turn(registry, tid)
        assert calls[1].resume == "s1"
        assert calls[2].resume == "s1"  # s2 never persisted -> skipped
        assert any(e["event"] == "resume_target_skipped" for e in events.read("w1"))
    finally:
        await engine.stop()


def test_session_transcript_path_sanitization():
    from worker_supervisor.engine import session_transcript_path

    p = session_transcript_path("/private/tmp/my_repo.x", "abc-123")
    assert p.name == "abc-123.jsonl"
    assert p.parent.name == "-private-tmp-my-repo-x"


async def test_auto_cycle_fires_on_context_pressure(make_engine, registry, repo, events):
    """FR-WS6: usage above the threshold auto-enqueues a cycle after a clean turn."""
    big = {"input_tokens": 150_000, "cache_read_input_tokens": 50_000}
    engine, _ = make_engine([[r("s1", usage=big)], [r("s2")], [r("s3")]])
    await engine.spawn("w1", str(repo))
    await engine.prompt("w1", "heavy context work")

    async def _cycled():
        epoch = await registry.current_epoch("w1")
        return epoch if epoch["seq"] == 2 else None

    await wait_until(_cycled)
    assert any(e["event"] == "auto_cycle" for e in events.read("w1"))


async def test_context_pressure_uses_last_request_usage(make_engine, registry, repo, events):
    """Pressure reads the LAST AssistantMessage's per-request usage, never
    ResultMessage's cumulative sum — a multi-call turn's sum can exceed the
    whole context window and would thrash auto-cycle (proven live)."""
    cumulative = {"input_tokens": 100, "cache_read_input_tokens": 322_000}
    last_request = {"input_tokens": 10, "cache_read_input_tokens": 40_000}
    engine, _ = make_engine(
        [[a("Bash", usage={"input_tokens": 5, "cache_read_input_tokens": 20_000}),
          a("Read", usage=last_request),
          r("s1", usage=cumulative)]]
    )
    await engine.spawn("w1", str(repo))
    turn_id = await engine.prompt("w1", "multi tool-call turn")

    async def _done():
        turn = await registry.get_turn(turn_id)
        return turn if turn["state"] == "done" else None

    turn = await wait_until(_done)
    import json as _json

    assert _json.loads(turn["usage"]) == last_request
    finished = [e for e in events.read("w1") if e["event"] == "turn_finished"]
    assert finished[-1]["context_pct"] == 20  # 40k/200k, not min(100, 322k/200k)
    assert not any(e["event"] == "auto_cycle" for e in events.read("w1"))


async def test_system_prompt_carries_live_limits(make_engine, registry, repo, cfg):
    """ClaudeAgentOptions.system_prompt must render live wall_clock_s / max_turns /
    cycle_context_pct so the agent can self-pace — never hardcoded (ECA-72 AC#2).

    Evidence: epoch-2 restores grounded at 69-79% context because the agent had no
    per-turn awareness of its limits; epoch-3 landed 44-45% under explicit guidance.
    """
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "check options")
    await terminal_turn(registry, tid)

    sp = calls[0].system_prompt
    assert sp is not None, "system_prompt must be set on every turn"
    assert sp["type"] == "preset"
    assert sp["preset"] == "claude_code"
    append = sp["append"]

    # Discriminating substrings: the exact phrases _discipline_append renders.
    assert f"{cfg.limits.wall_clock_s}s wall-clock" in append
    assert f"{cfg.limits.max_turns} SDK turns" in append
    assert str(cfg.cycle_context_pct) in append

    # The three discipline clauses must be present.
    assert "Commit completed work BEFORE" in append
    assert "nohup" in append
