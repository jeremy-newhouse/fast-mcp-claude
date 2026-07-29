"""Turn engine behavior against a scripted fake `query`: resume chaining, the
failure ladder (retry-once / resume_failed), budget refusal, cycling (manual +
auto), and lifecycle records — the code-provable halves of AC-WS-1/4/5/11."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ProcessError,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
)

from worker_supervisor.engine import Engine, _discipline_append
from worker_supervisor.config import Limits
from worker_supervisor.gate import QuestionBridge, WorkerPolicy
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
        if options.stderr is not None:
            options.stderr("mock cli stderr line")
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


async def test_turn_mcp_diagnostics_emitted_on_failure_ladder_too(
    make_engine, registry, repo, events
):
    """ECA-101 AC1 (adversarial-review follow-up): the FAILURE ladder (_fail_turn)
    must surface MCP diagnostics too, not just a successful turn — a granted
    server that never finishes connecting is exactly the kind of failure an
    operator most wants this evidence for, and the pre-existing failure capsule
    (Amendment A6) carries no MCP-specific context at all."""
    engine, _ = make_engine([RuntimeError("boom 1"), RuntimeError("boom 2"), [r("sX")]])
    await engine.spawn(
        "w1", str(repo), WorkerPolicy(mcp_servers={"context7": {"type": "stdio"}})
    )
    tid = await engine.prompt("w1", "doomed")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "error"

    diag = [e for e in events.read("w1") if e["event"] == "turn_mcp_diagnostics"]
    assert len(diag) == 1
    assert diag[0]["granted"] == ["context7"]


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


async def test_lifecycle_turn_runs_despite_exhausted_budget(make_engine, registry, repo):
    """ECA-99: an exhausted epoch must NOT refuse a lifecycle (cycle_handover) turn —
    otherwise the lane can never cycle out (the handover-write lives in the exhausted
    epoch). The cycle runs, rolls the epoch, and its SDK budget floors to the lifecycle
    reserve instead of the $0.01 no-op floor a normal over-budget turn would get."""
    engine, calls = make_engine([[r("s1", cost=2.0)], [r("s2")], [r("s3")]])  # cap is 1.0
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "expensive")
    await terminal_turn(registry, t1)  # epoch now exhausted (2.0 > 1.0 cap)
    await engine.cycle("w1")

    async def _cycled():
        epoch = await registry.current_epoch("w1")
        return epoch if epoch["seq"] == 2 else None

    await wait_until(_cycled)  # hangs (timeout) if the cycle_handover was budget_refused

    cycle_turns = [
        t for t in await registry.history("w1", limit=10) if t["kind"] == "cycle_handover"
    ]
    assert cycle_turns and cycle_turns[0]["state"] == "done"
    assert calls[1].max_budget_usd == pytest.approx(5.0)  # lifecycle reserve, not 0.01


async def test_remove_requires_terminal_then_frees_name(make_engine, registry, repo):
    """ECA-99: engine.remove refuses a live worker; after kill it purges the row so the
    same name re-spawns (kill alone keeps the PK row and blocks respawn)."""
    engine, _ = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo))
    with pytest.raises(ValueError, match="kill it before remove"):
        await engine.remove("w1")  # idle worker is live — refused
    await engine.kill("w1")
    await engine.remove("w1")
    assert await registry.get_worker("w1") is None
    again = await engine.spawn("w1", str(repo))  # name freed
    assert again["name"] == "w1" and again["status"] == "idle"


async def test_manual_cycle_rolls_epoch_restores_and_continues(make_engine, registry, repo, events):
    # ECA-84: the restore turn only re-grounds; the supervisor then auto-enqueues one
    # continuation work-turn (kind='prompt') so autonomous work proceeds under a fresh
    # budget. Expected chain: prompt -> cycle_handover -> restore -> prompt (continuation).
    engine, _ = make_engine([[r("s1")], [r("s2")], [r("s3")], [r("s4")]])
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "work")
    await terminal_turn(registry, t1)
    await engine.cycle("w1")

    async def _cycled():
        epoch = await registry.current_epoch("w1")
        return epoch if epoch["seq"] == 2 else None

    await wait_until(_cycled)

    async def _continued():
        rows = list(reversed(await registry.history("w1", limit=10)))
        kinds = [t["kind"] for t in rows]
        if kinds == ["prompt", "cycle_handover", "restore", "prompt"] and rows[-1]["state"] == "done":
            return kinds
        return None

    kinds = await wait_until(_continued)
    assert kinds == ["prompt", "cycle_handover", "restore", "prompt"]
    assert any(e["event"] == "epoch_cycled" for e in events.read("w1"))
    assert any(e["event"] == "restore_continued" for e in events.read("w1"))


async def test_restore_continuation_is_guarded_by_a_pending_queue(
    make_engine, registry, repo, events, monkeypatch
):
    """ECA-84: a bounded restore auto-enqueues ONE continuation only when the queue is
    empty. If the orchestrator already queued its own next prompt, we must not stack a
    racing continuation behind it. Driven by calling _after_turn directly with _kick
    stubbed, so the assertion is on the enqueue decision, not on loop timing."""
    engine, _ = make_engine([[r("s1")]])
    monkeypatch.setattr(engine, "_kick", lambda name: None)  # isolate the enqueue decision
    await engine.spawn("w1", str(repo))

    async def _finished_restore() -> int:
        tid = await registry.enqueue_turn("w1", "restore", kind="restore")
        await registry.claim_turn(tid)
        await registry.start_turn(tid, None)
        await registry.finish_turn(tid, "done", session_id=f"s{tid}")
        return tid

    # Empty queue -> exactly one continuation prompt is enqueued.
    await engine._after_turn("w1", await _finished_restore())
    queued = [
        t for t in await registry.history("w1", limit=20)
        if t["kind"] == "prompt" and t["state"] == "queued"
    ]
    assert len(queued) == 1
    assert any(e["event"] == "restore_continued" for e in events.read("w1"))

    # Non-empty queue (the continuation above is still pending) -> a second bounded
    # restore adds NOTHING; the guard defers to the already-pending work.
    before = len(await registry.history("w1", limit=50))
    await _finished_restore()
    after = len(await registry.history("w1", limit=50))
    assert after == before + 1  # only the restore row itself; no extra continuation
    assert sum(e["event"] == "restore_continued" for e in events.read("w1")) == 1


async def test_resume_skips_unpersisted_session(cfg, registry, events, repo, monkeypatch):
    """Gotcha observed on CLI 2.1.165 (see _pick_resume_target; not re-measured on
    the 2.1.220 bundle): a reported session id may never reach disk — resume the
    newest PERSISTED id instead of erroring the whole epoch."""
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

    # The four discipline clauses must be present.
    assert "Commit completed work BEFORE" in append
    assert "nohup" in append
    assert "Run allowlisted commands" in append
    assert "PLAINLY" in append

    # No MCP grant on this lane (default WorkerPolicy()) -> no MCP clause at all.
    assert "MCP SERVERS" not in append


def test_discipline_append_mcp_retry_hint():
    """ECA-101 AC3 mitigation: a granted-MCP lane's system prompt must tell the
    agent that a first ToolSearch/tool-call miss for one of ITS granted servers
    can be the startup race, not real unavailability, and to retry once."""
    limits = Limits(wall_clock_s=100, max_turns=10)

    no_mcp = _discipline_append(limits, 80, [])
    assert "MCP SERVERS" not in no_mcp

    with_mcp = _discipline_append(limits, 80, ["context7", "langfuse"])
    assert "MCP SERVERS" in with_mcp
    assert "context7, langfuse" in with_mcp
    assert "retry once" in with_mcp


async def test_turn_mcp_diagnostics_emitted_on_success(make_engine, registry, repo, events):
    """ECA-101 AC1: previously only a FAILED turn's stderr/mcp state ever reached
    a capsule (_finish_failure_capsule) — a turn that SUCCEEDS while a granted MCP
    server never finished connecting left zero evidence anywhere. A 'done' turn on
    an MCP-granted lane must now surface the init snapshot + raw stderr tail too."""
    init_msg = SystemMessage(
        subtype="init", data={"mcp_servers": [{"name": "context7", "status": "pending"}]}
    )
    engine, calls = make_engine([[init_msg, r("s1")]])
    await engine.spawn(
        "w1", str(repo), WorkerPolicy(mcp_servers={"context7": {"type": "stdio"}})
    )
    tid = await engine.prompt("w1", "do the thing")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "done"

    diag = [e for e in events.read("w1") if e["event"] == "turn_mcp_diagnostics"]
    assert len(diag) == 1
    assert diag[0]["granted"] == ["context7"]
    assert diag[0]["mcp_init"] == [{"name": "context7", "status": "pending"}]
    assert diag[0]["stderr_tail"] == ["mock cli stderr line"]

    # The lane's own system prompt must carry the retry-hint naming ITS servers.
    append = calls[0].system_prompt["append"]
    assert "MCP SERVERS" in append and "context7" in append


async def test_turn_mcp_diagnostics_absent_without_mcp_grant(make_engine, registry, repo, events):
    """A lane with no MCP grant at all gets no diagnostics noise."""
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo))  # default WorkerPolicy(): no mcp_servers
    tid = await engine.prompt("w1", "do the thing")
    await terminal_turn(registry, tid)
    assert not any(e["event"] == "turn_mcp_diagnostics" for e in events.read("w1"))


async def test_mcp_startup_grace_delays_prompt_only_when_servers_granted(
    cfg, registry, events, monkeypatch, repo
):
    """ECA-101 AC3: query()'s own pre-first-message wait only ever covers
    'sdk'-type (in-process) mcp_servers, never the stdio/http/https servers a
    worker policy actually grants — so without a deliberate grace period, those
    servers get zero guaranteed head start against the model's first action."""
    grace_cfg = dataclasses.replace(cfg, mcp_startup_grace_s=0.2)
    calls: list[Any] = []
    monkeypatch.setattr(
        "worker_supervisor.engine.query", make_fake_query([[r("s1")]], calls)
    )
    monkeypatch.setattr(Engine, "_transcript_exists", lambda self, cwd, sid: True)
    bridge = QuestionBridge(registry, events)
    engine = Engine(grace_cfg, registry, events, bridge)
    try:
        await engine.spawn(
            "w1", str(repo), WorkerPolicy(mcp_servers={"context7": {"type": "stdio"}})
        )
        start = asyncio.get_event_loop().time()
        tid = await engine.prompt("w1", "do the thing")
        await terminal_turn(registry, tid)
        assert asyncio.get_event_loop().time() - start >= 0.2
    finally:
        await engine.stop()


async def test_mcp_startup_grace_skipped_without_mcp_grant(
    cfg, registry, events, monkeypatch, repo
):
    """The grace period must not tax lanes that were never granted any MCP server."""
    grace_cfg = dataclasses.replace(cfg, mcp_startup_grace_s=0.2)
    calls: list[Any] = []
    monkeypatch.setattr(
        "worker_supervisor.engine.query", make_fake_query([[r("s1")]], calls)
    )
    monkeypatch.setattr(Engine, "_transcript_exists", lambda self, cwd, sid: True)
    bridge = QuestionBridge(registry, events)
    engine = Engine(grace_cfg, registry, events, bridge)
    try:
        await engine.spawn("w1", str(repo))  # no mcp_servers granted
        start = asyncio.get_event_loop().time()
        tid = await engine.prompt("w1", "do the thing")
        await terminal_turn(registry, tid)
        assert asyncio.get_event_loop().time() - start < 0.2
    finally:
        await engine.stop()


# --- ECA-135: granted MCP credentials must never enter the child's argv ---------
#
# Confirmed live on mbpm2 (CLI 2.1.220 / SDK 0.2.91, 2026-07-28) before the fix: a
# worker turn read both of its own planted sentinels out of its parent's argv with
# one `ps`, and `workers get` handed them back to the caller. These tests exercise
# the real SDK argv builder, so they fail against the pre-fix engine rather than
# merely restating what the new code does.

ECA135_SENTINEL = "ECA135-SENTINEL-vd83ka-not-a-real-credential"


def granted_policy() -> WorkerPolicy:
    return WorkerPolicy(
        mcp_servers={
            "paid": {
                "type": "http",
                "url": "http://127.0.0.1:1/mcp",
                "headers": {"Authorization": f"Bearer {ECA135_SENTINEL}"},
            }
        }
    )


def capture_config_state(monkeypatch, sink: dict) -> None:
    """Snapshot the credential file as the CLI sees it — DURING the turn.

    _worker_loop unlinks it the moment the turn ends (that transience is the point,
    and its own test), so anything asserted about the file itself has to be captured
    while it is live rather than read back afterwards."""
    original = Engine._write_mcp_config

    def spy(self, worker, turn_id, servers):
        arg = original(self, worker, turn_id, servers)
        if isinstance(arg, str):
            path = Path(arg)
            sink[worker] = {
                "path": path,
                "content": path.read_text(),
                "mode": stat.S_IMODE(path.stat().st_mode),
                "dir_mode": stat.S_IMODE(path.parent.stat().st_mode),
            }
        return arg

    monkeypatch.setattr(Engine, "_write_mcp_config", spy)


def sdk_argv(options: Any) -> list[str]:
    """The argv the REAL SDK would exec for these options, spawning nothing."""
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    resolved = dataclasses.replace(options, cli_path="/nonexistent/claude")
    return SubprocessCLITransport(prompt="x", options=resolved)._build_command()


async def test_granted_mcp_credentials_never_reach_the_cli_argv(
    make_engine, registry, repo
):
    """The load-bearing assertion: the secret is absent from the command line the
    SDK would exec, and what replaces it is the config PATH."""
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    argv = sdk_argv(calls[0])
    assert ECA135_SENTINEL not in " ".join(argv)
    assert argv[argv.index("--mcp-config") + 1] == calls[0].mcp_servers
    assert isinstance(calls[0].mcp_servers, str)


async def test_mcp_config_file_carries_the_policy_at_0600_in_a_0700_dir(
    make_engine, registry, repo, monkeypatch
):
    """Moving the credential from argv to disk is only a gain if the file is not
    itself loosely permissioned — and the content must still be what the CLI expects."""
    seen: dict = {}
    capture_config_state(monkeypatch, seen)
    engine, calls = make_engine([[r("s1")]])
    policy = granted_policy()
    await engine.spawn("w1", str(repo), policy)
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    live = seen["w1"]
    assert live["path"] == Path(calls[0].mcp_servers)
    assert json.loads(live["content"]) == {"mcpServers": policy.mcp_servers}
    assert live["mode"] == 0o600
    # NB: on a fresh dir under a tight umask (this dev host runs 077) mkdir alone
    # already yields 0700, so this line does not by itself prove the explicit
    # chmod is there — test_mcp_config_write_tightens_a_pre_existing_loose_file
    # is the one that pins it umask-independently. Kept as the end-state assertion.
    assert live["dir_mode"] == 0o700


async def test_a_pre_existing_loose_config_dir_is_tightened(
    make_engine, registry, repo, cfg, monkeypatch
):
    """mkdir's mode is umask-masked and skipped entirely when the directory already
    exists, so a dir left at 0755 by anything else must still be tightened. This is the
    assertion that pins the explicit chmod umask-independently — on a fresh dir under a
    tight umask (this dev host runs 077) mkdir alone already yields 0700."""
    cfg.mcp_config_dir.mkdir(parents=True)
    cfg.mcp_config_dir.chmod(0o755)

    seen: dict = {}
    capture_config_state(monkeypatch, seen)
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert seen["w1"]["dir_mode"] == 0o700
    assert seen["w1"]["mode"] == 0o600
    assert ECA135_SENTINEL in seen["w1"]["content"]


async def test_the_config_mode_does_not_depend_on_the_open_mode(
    make_engine, registry, repo, monkeypatch
):
    """O_EXCL means the file is always brand-new, so O_CREAT's 0600 normally governs and
    the explicit fchmod is belt-and-braces — for a filesystem that applies inherited
    ACLs or otherwise ignores the open mode. Simulate one, so the fchmod is pinned by
    something rather than carried on faith."""
    real_open, real_umask = os.open, os.umask
    monkeypatch.setattr(
        "worker_supervisor.engine.os.open",
        lambda path, flags, mode=0o777: real_open(path, flags, 0o666),
    )
    # Without this the host umask (077 here) masks the simulated loose mode back down
    # to 0600 on its own and the assertion below passes with no fchmod at all.
    old_umask = os.umask(0)
    monkeypatch.setattr(os, "umask", lambda m: old_umask)  # keep pytest teardown honest
    seen: dict = {}
    capture_config_state(monkeypatch, seen)
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    try:
        await terminal_turn(registry, await engine.prompt("w1", "go"))
    finally:
        real_umask(old_umask)

    assert seen["w1"]["mode"] == 0o600


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
async def test_a_pre_planted_link_never_receives_the_credential(
    make_engine, registry, repo, cfg, tmp_path, monkeypatch, link
):
    """The round-1 fix used a derived (therefore predictable) filename, and O_NOFOLLOW
    rejects symlinks while saying nothing about HARD links — a hard link planted at that
    path took the whole credential write: O_TRUNC destroying the target, fchmod setting
    it 0600, and the credential surviving at the attacker's path even after the turn-end
    unlink, which removes the link and not the inode.

    Two changes kill the class outright rather than by enumerating link types: the
    filename carries a random component (so it cannot be predicted), and the open is
    O_EXCL after a purge (so the daemon never opens an inode it did not just create).
    Whatever was planted is unlinked as a leftover, which for a link removes the LINK,
    never the victim."""
    victim = tmp_path / "victim.json"
    victim.write_text('{"operator": "config"}')
    victim.chmod(0o644)
    cfg.mcp_config_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "worker_supervisor.engine.secrets.token_hex", lambda n: "deadbeefdeadbeef"
    )
    # The turn id is assigned by the registry, so plant across the plausible range.
    for tid in range(1, 6):
        planted = cfg.mcp_config_dir / f"w1-{tid}-deadbeefdeadbeef.json"
        if link == "symlink":
            planted.symlink_to(victim)
        else:
            os.link(victim, planted)

    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    turn = await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert turn["state"] == "done"
    assert victim.read_text() == '{"operator": "config"}', "the victim was written!"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644, "the victim was chmod'd!"
    assert ECA135_SENTINEL not in victim.read_text()


async def test_lane_without_an_mcp_grant_is_byte_for_byte_unchanged(
    make_engine, registry, repo, cfg
):
    """Negative control: the un-granted lane keeps the empty-dict option and gets no
    --mcp-config at all, so this fix cannot regress the lanes it does not concern."""
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo))  # no mcp_servers granted
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert calls[0].mcp_servers == {}
    assert "--mcp-config" not in sdk_argv(calls[0])
    assert not cfg.mcp_config_dir.exists()


async def test_retry_ladder_writes_the_config_once(
    make_engine, registry, repo, monkeypatch
):
    """Written once per TURN, before the attempt loop. The path alone cannot show
    this — it is derived from the worker name, so a per-attempt write would land on
    the same path — so count the writes."""
    writes: list[str] = []
    original = Engine._write_mcp_config

    def counting(self, worker, turn_id, servers):
        writes.append(worker)
        return original(self, worker, turn_id, servers)

    monkeypatch.setattr(Engine, "_write_mcp_config", counting)

    engine, calls = make_engine([RuntimeError("boom 1"), [r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert len(calls) == 2, "expected the retry ladder to run two attempts"
    assert writes == ["w1"]
    assert isinstance(calls[0].mcp_servers, str)
    assert calls[0].mcp_servers == calls[1].mcp_servers


async def test_remove_sweeps_a_config_file_left_by_a_crashed_daemon(
    make_engine, registry, repo, cfg
):
    """With the turn-end unlink in place, `remove` covers exactly one case: a daemon
    that died mid-turn and left the file behind. Stage that state directly — asserting
    it after a NORMAL turn would assert nothing, since the file is already gone."""
    engine, _ = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    orphan = cfg.mcp_config_dir / "w1-99-deadbeefdeadbeef.json"
    assert not list(cfg.mcp_config_dir.iterdir()), "the turn-end purge should have run"
    orphan.write_text('{"mcpServers": {}}')  # simulate the crash leftover

    await engine.kill("w1")
    await engine.remove("w1")
    assert not orphan.exists()


# --- ECA-135, second round: the gaps the adversarial review found ---------------


async def test_strict_mcp_config_still_rides_with_the_grant(
    make_engine, registry, repo
):
    """The flag that stops an ambient repo .mcp.json widening a lane's surface sits one
    line from the code this change rewrote, and NOTHING in the suite pinned it —
    deleting it outright left 67/67 green. It matters more with a file config, not
    less: a corrupt file plus strict mode means a granted lane silently runs with zero
    servers rather than erroring."""
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert calls[0].strict_mcp_config is True
    assert "--strict-mcp-config" in sdk_argv(calls[0])


async def test_two_lanes_get_separate_files_and_remove_is_surgical(
    make_engine, registry, repo, cfg, monkeypatch
):
    """Cross-lane isolation of the FILES was pinned only incidentally, by another test
    hardcoding 'w1.json' — and cross-lane is the whole subject of ECA-135.

    Note what this does NOT claim: the files are 0600 but every lane runs as the same
    uid, so lane B can still read lane A's file. This asserts that the daemon keeps
    them SEPARATE and that removing one leaves the other, not that the OS keeps them
    apart. It does not."""
    seen: dict = {}
    capture_config_state(monkeypatch, seen)
    engine, calls = make_engine([[r("s1")], [r("s2")]])
    a_policy = granted_policy()  # carries ECA135_SENTINEL
    b_policy = WorkerPolicy(
        mcp_servers={"other": {"type": "stdio", "command": "/bin/true"}}
    )
    await engine.spawn("lanea", str(repo), a_policy)
    await engine.spawn("laneb", str(repo), b_policy)
    await terminal_turn(registry, await engine.prompt("lanea", "go"))
    await terminal_turn(registry, await engine.prompt("laneb", "go"))

    # CONTENT, not just paths: comparing filenames cannot see the failure that
    # actually matters here — lane B's file holding lane A's bearer.
    assert ECA135_SENTINEL in seen["lanea"]["content"]
    assert ECA135_SENTINEL not in seen["laneb"]["content"]
    assert json.loads(seen["laneb"]["content"]) == {"mcpServers": b_policy.mcp_servers}
    assert seen["lanea"]["path"] != seen["laneb"]["path"]
    assert not seen["lanea"]["path"].exists() and not seen["laneb"]["path"].exists()

    await engine.kill("lanea")
    await engine.remove("lanea")
    assert await registry.get_worker("laneb") is not None


async def test_config_file_does_not_outlive_its_turn(make_engine, registry, repo, cfg):
    """Argv's one virtue was being transient. A file that persisted past the turn — let
    alone past `kill` — would trade a turn-scoped exposure for a permanent one."""
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert not Path(calls[0].mcp_servers).exists()
    assert list(cfg.mcp_config_dir.iterdir()) == []


async def test_mcp_config_write_failure_fails_the_turn_instead_of_wedging_the_lane(
    make_engine, registry, repo, cfg, events
):
    """Regression found in review: the write is the first filesystem touch _run_turn
    makes, and it sits OUTSIDE the attempt loop's handlers. Nothing supervises a runner
    task, so an escaping OSError killed the lane's loop — turn stuck `claimed`, worker
    stuck `running`, no event, no capsule, forever. It must fail the TURN."""
    cfg.mcp_config_dir.parent.mkdir(parents=True, exist_ok=True)
    cfg.mcp_config_dir.write_text("not a directory")  # mkdir(exist_ok=True) -> OSError

    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    turn = await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert turn["state"] == "error"
    assert "mcp config write failed" in turn["error"]
    assert calls == [], "the CLI must never have been spawned"
    assert (await registry.get_worker("w1"))["status"] == "idle"
    assert [e for e in events.read("w1") if e["event"] == "turn_error"]
    assert list(cfg.capsules_dir.glob("w1-turn*.json")), "failure capsule missing"
    # NB: this is a cheap smoke check, NOT the guard against the wedge — terminal_turn
    # returns before the loop's finally, so it can pass with the runner already dying.
    # test_a_failing_turn_end_purge_cannot_kill_the_worker_loop is the one that bites
    # (verified by mutation); an earlier version of this comment claimed otherwise.
    assert not engine._runners["w1"].done(), "the runner loop died"


@pytest.mark.parametrize(
    "bad",
    ["../../.claude", "a/b", ".hidden", "..", "", "x" * 65, "na me", "w1\n", "w1\nx"],
)
async def test_spawn_rejects_names_that_are_not_safe_filenames(
    make_engine, repo, bad
):
    """A worker name becomes a filename, and since this change that filename is
    TRUNCATED and UNLINKED. Review demonstrated `../../.claude` overwriting the
    operator's real ~/.claude.json — the file the deploy generator reads live
    credentials out of."""
    engine, _ = make_engine([[r("s1")]])
    with pytest.raises(ValueError, match="invalid worker name"):
        await engine.spawn(bad, str(repo), granted_policy())


async def test_spawn_rejects_a_case_folded_collision(make_engine, repo):
    """APFS is case-insensitive; the registry's TEXT PRIMARY KEY is not. Two distinct
    workers would share one credential file and the last writer would win — a lane
    starting with another lane's bearers, which is exactly the leak this task is
    about."""
    engine, _ = make_engine([[r("s1")]])
    await engine.spawn("Ultra1", str(repo), granted_policy())
    with pytest.raises(ValueError, match="collides case-insensitively"):
        await engine.spawn("ultra1", str(repo), granted_policy())


async def test_a_symlinked_config_DIRECTORY_is_refused_not_followed(
    make_engine, registry, repo, cfg, tmp_path
):
    """Path.mkdir(exist_ok=True) accepts a symlink-to-directory and Path.chmod then
    chmods the TARGET — so without the explicit is_symlink guard the daemon would
    relocate a lane's credentials into an attacker-chosen directory and set it 0700."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o755)
    # A victim that MATCHES the purge glob. Without it the target is empty, so a purge
    # that follows the symlink and a purge that refuses look identical — which is how
    # the first version of this test missed a confused-deputy unlink.
    victim = elsewhere / "w1-1-deadbeefdeadbeef.json"
    victim.write_text('{"operator": "config"}')
    cfg.mcp_config_dir.parent.mkdir(parents=True, exist_ok=True)
    cfg.mcp_config_dir.symlink_to(elsewhere)

    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    turn = await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert turn["state"] == "error"
    assert victim.read_text() == '{"operator": "config"}', "the victim was deleted!"
    assert list(elsewhere.iterdir()) == [victim], "credentials landed in the target"
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755, "target was chmod'd"
    assert calls == []


# --- ECA-135 round 3: guards the round-2 tests could not see --------------------


async def test_a_failing_turn_end_purge_cannot_kill_the_worker_loop(
    make_engine, registry, repo, cfg, events, monkeypatch
):
    """The purge runs in `_worker_loop`'s finally, BEFORE `_after_turn`. An escape there
    kills the runner and silently stops all lifecycle chaining — no epoch roll, no
    restore enqueue, no auto-cycle — while the turn still reports success. That is the
    same wedge this task fixed at the write site and then reintroduced six lines above
    it; both reviewers found it independently.

    Make the purge genuinely fail: strip write permission from the config dir once the
    file is in place, so the unlink gets EACCES."""
    original = Engine._write_mcp_config

    def write_then_lock_the_dir(self, worker, turn_id, servers):
        arg = original(self, worker, turn_id, servers)
        self._cfg.mcp_config_dir.chmod(0o500)  # r-x: unlink now raises EACCES
        return arg

    monkeypatch.setattr(Engine, "_write_mcp_config", write_then_lock_the_dir)

    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    turn = await terminal_turn(registry, await engine.prompt("w1", "go"))
    try:
        assert turn["state"] == "done"

        # WAIT for the purge to have happened. `terminal_turn` returns as soon as the
        # turn row is terminal, which _run_turn writes BEFORE the loop's finally — so
        # asserting on the runner here would be a race, and would pass whether or not
        # the exception escapes. Waiting on the event is the deterministic form: if the
        # handler is gone, the exception escapes before the emit and this times out.
        async def _purge_failed():
            hits = [
                e for e in events.read("w1") if e["event"] == "mcp_config_purge_failed"
            ]
            return hits or None

        failures = await wait_until(_purge_failed)
        assert "PermissionError" in failures[0]["error"]
        assert not engine._runners["w1"].done(), "the runner loop died on cleanup"
    finally:
        cfg.mcp_config_dir.chmod(0o700)


async def test_a_link_planted_after_the_purge_is_still_refused(
    make_engine, registry, repo, cfg, tmp_path, monkeypatch
):
    """O_EXCL is the last line of defence, and the purge normally clears the path before
    it — so nothing pins O_EXCL unless the plant lands in the window BETWEEN them. Use
    the daemon's own `chmod` on the config dir as that seam: it runs after the purge and
    before the open."""
    victim = tmp_path / "victim.json"
    victim.write_text('{"operator": "config"}')
    monkeypatch.setattr(
        "worker_supervisor.engine.secrets.token_hex", lambda n: "deadbeefdeadbeef"
    )
    real_chmod = Path.chmod

    def chmod_then_plant(self, mode, **kw):
        real_chmod(self, mode, **kw)
        if self == cfg.mcp_config_dir:
            for tid in range(1, 6):
                target = cfg.mcp_config_dir / f"w1-{tid}-deadbeefdeadbeef.json"
                if not target.exists():
                    os.link(victim, target)

    monkeypatch.setattr(Path, "chmod", chmod_then_plant)

    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    turn = await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert turn["state"] == "error"
    assert "File exists" in turn["error"] or "EEXIST" in turn["error"]
    assert victim.read_text() == '{"operator": "config"}', "the victim was written!"
    assert calls == [], "the CLI must never have been spawned"


async def test_the_config_filename_is_not_predictable(
    make_engine, registry, repo, monkeypatch
):
    """Unpredictability is what makes pre-planting impossible, so it is a security
    property and not a detail: a name derived from the worker alone is guessable from
    `workers status` or the mesh announcement."""
    paths: list[Path] = []
    original = Engine._write_mcp_config

    def record(self, worker, turn_id, servers):
        arg = original(self, worker, turn_id, servers)
        paths.append(Path(arg))
        return arg

    monkeypatch.setattr(Engine, "_write_mcp_config", record)
    engine, calls = make_engine([[r("s1")], [r("s2")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "one"))
    await terminal_turn(registry, await engine.prompt("w1", "two"))

    assert len(paths) == 2
    tokens = [p.stem.rsplit("-", 1)[-1] for p in paths]
    assert tokens[0] != tokens[1], "the filename repeats across turns"
    assert all(len(t) == 16 and set(t) <= set("0123456789abcdef") for t in tokens)


async def test_boot_sweeps_credential_files_a_killed_daemon_left_behind(
    make_engine, registry, repo, cfg
):
    """A SIGKILLed daemon runs no `finally`, and an orphan for a lane never prompted
    again would sit there indefinitely — the permanent exposure this fix exists to
    avoid. No turn can be in flight at boot, so everything present is an orphan."""
    engine, _ = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    cfg.mcp_config_dir.mkdir(parents=True, exist_ok=True)
    orphan = cfg.mcp_config_dir / "w1-7-deadbeefdeadbeef.json"
    orphan.write_text('{"mcpServers": {"paid": {"headers": {"x": "leftover"}}}}')

    await engine.start()

    assert not orphan.exists()


async def test_purging_a_hostile_persisted_name_cannot_escape_the_directory(
    make_engine, repo, tmp_path, events
):
    """Spawn-time validation does not cover a row persisted BEFORE that validator
    existed, and `remove`, the turn-end purge and boot recovery all consume a stored
    name as a path. Hence the guard sits at path derivation, where every consumer passes
    through — verified here by calling the purge directly with a name `spawn` would now
    reject."""
    victim = tmp_path / "outside.json"
    victim.write_text("operator data")

    engine, _ = make_engine([[r("s1")]])
    before = set(tmp_path.iterdir())
    engine._purge_mcp_config("../../outside")  # must not raise, must not delete

    assert victim.exists(), "the purge escaped the config directory"
    refused = [e for e in events.read("-") if e["event"] == "mcp_config_purge_refused"]
    assert refused and refused[-1]["lane"] == "../../outside"
    # ...and the REFUSAL must not itself escape: EventLog names its file after the key
    # it is given, so emitting under the hostile name would write outside the home.
    assert set(tmp_path.iterdir()) == before, "the refusal event escaped the directory"
    # ECA-137: pins that this caller passes the DAEMON KEY deliberately. Since EventLog
    # now re-keys a refused key itself, emitting under `worker` here would land in the
    # same file and satisfy every assertion above — mutation-testing found exactly that
    # mutant surviving. An absent `log_key_refused` is what distinguishes the two.
    assert "log_key_refused" not in refused[-1]


# --- ECA-135 round 4: findings the round-3 suite passed straight through --------


async def test_a_hostile_persisted_name_cannot_plant_a_config_outside_the_home(
    make_engine, registry, repo, tmp_path, cfg
):
    """Round 2 fixed the traversal UNLINK; this is the traversal WRITE, which replaced
    it. `_write_mcp_config` derived its path directly, and the purge on the line above
    validates but SWALLOWS the refusal by design — so it could never be what protects
    this path. A row persisted before spawn-time validation existed would plant a
    credential file outside the supervisor home, where neither the purge nor the boot
    sweep can ever reach it."""
    hostile = "../../escaped"
    await registry.spawn_worker(  # bypasses Engine.spawn, as a legacy row does
        hostile, str(repo), json.loads(granted_policy().to_json())
    )
    cfg.mcp_config_dir.mkdir(parents=True, exist_ok=True)

    engine, calls = make_engine([[r("s1")]])
    engine._ensure_runner(hostile)
    turn = await terminal_turn(registry, await engine.prompt(hostile, "go"))

    assert turn["state"] == "error"
    assert "invalid worker name" in turn["error"]
    assert calls == [], "the CLI must never have been spawned"

    # No CREDENTIAL-bearing file anywhere outside the config dir. Asserted on content
    # rather than on filenames because when this test was written the same error path
    # DID write a failure capsule through the hostile name — a separate pre-existing
    # traversal in capsule.py/events.py, which a filename-based assertion here would
    # have failed on without being this defect. ECA-137 has since closed that one, so
    # nothing escapes on this path any more (proved by its own test below); the
    # content-based assertion is kept because it is the narrower claim, and it is what
    # this test is actually about.
    outside = cfg.mcp_config_dir.parent.parent
    leaked = [
        f for f in outside.rglob("*.json")
        if cfg.mcp_config_dir not in f.parents and ECA135_SENTINEL in f.read_text()
    ]
    assert leaked == [], f"a credential was planted outside the config dir: {leaked}"


async def test_purging_one_lane_leaves_a_prefix_named_lanes_config_alone(
    make_engine, registry, repo, cfg
):
    """'-' is the field separator AND a legal name character, so a bare `<worker>-*.json`
    glob also matches a lane whose name EXTENDS this one. Purging `ultra` would delete
    `ultra-2`'s live in-flight config, and under strict_mcp_config that lane then runs
    with zero MCP servers and no error at all — a cross-lane break introduced by the
    cleanup that exists to prevent cross-lane leakage."""
    engine, _ = make_engine([[r("s1")]])
    cfg.mcp_config_dir.mkdir(parents=True, exist_ok=True)
    mine = cfg.mcp_config_dir / "ultra-1-aaaaaaaaaaaaaaaa.json"
    theirs = cfg.mcp_config_dir / "ultra-2-1-bbbbbbbbbbbbbbbb.json"
    mine.write_text("{}")
    theirs.write_text("{}")

    engine._purge_mcp_config("ultra")

    assert not mine.exists()
    assert theirs.exists(), "purging 'ultra' deleted lane 'ultra-2's live config"


async def test_the_purge_survives_a_failing_event_emit(
    make_engine, registry, repo, cfg, monkeypatch
):
    """`emit` is itself an unguarded file open, so an emit from INSIDE a handler can
    escape and re-create the wedge this method exists to prevent — the docstring's
    'NEVER raises' is the load-bearing part of the contract."""
    engine, _ = make_engine([[r("s1")]])
    cfg.mcp_config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        engine._events, "emit", lambda *a, **k: (_ for _ in ()).throw(OSError("logs gone"))
    )

    engine._purge_mcp_config("../../hostile")  # refusal path -> emit raises
    engine._purge_mcp_config("w1")  # ordinary path


def test_boot_refuses_a_second_daemon_before_touching_the_config_dir():
    """The sweep deletes every file in the MCP config dir on the premise that no turn can
    be in flight at boot — a premise the socket preflight is what actually enforces. With
    the sweep first, a mistakenly-started second instance would delete the LIVE daemon's
    in-flight credential files before discovering it should not have booted (the
    2026-07-07 double-daemon incident, now with a destructive action ahead of the check).

    Scope of what the ordering buys: the preflight keys on the SOCKET, so two daemons
    sharing a HOME with different SUPERVISOR_SOCKET overrides both pass it. See
    _sweep_orphan_mcp_configs — the premise is one daemon per HOME, and this check only
    delivers one per socket.
    """
    import worker_supervisor.__main__ as daemon_main

    source = Path(daemon_main.__file__).read_text()  # not a CWD-relative path
    assert source.index("preflight_socket_check") < source.index("await engine.start()")


async def test_a_broken_config_dir_is_reported_not_silently_skipped(
    make_engine, cfg, events
):
    """`Path.glob` swallows ENOTDIR and returns nothing, so a config dir that exists as a
    FILE looked like 'nothing to purge' and emitted no diagnostic at all. A granted lane
    still surfaces it through its turn error; an un-granted lane gave zero signal."""
    engine, _ = make_engine([[r("s1")]])
    cfg.mcp_config_dir.parent.mkdir(parents=True, exist_ok=True)
    cfg.mcp_config_dir.write_text("not a directory")

    engine._purge_mcp_config("w1")  # must not raise

    refused = [e for e in events.read("-") if e["event"] == "mcp_config_purge_refused"]
    assert refused and "not a directory" in refused[-1]["error"]


# --- ECA-137: the two writers ECA-135 left, closed at path derivation ------------


async def test_a_hostile_persisted_name_cannot_write_a_log_or_capsule_outside_the_home(
    make_engine, registry, repo, tmp_path, cfg, events
):
    """The escape this task was filed for, end to end.

    ECA-135 validated `Engine.spawn` and the MCP-config path, so no NEW row can carry a
    hostile name — but `EventLog` and `write_capsule` still derived filenames from a
    PERSISTED one, and a row written before that validator existed still reaches them.
    Demonstrated at the time: an ECA-135 test produced a capsule at
    `<home>/../../escaped-turn1-<ts>.json`.

    Driven with an UNGRANTED policy on purpose. A granted lane fails inside
    `_write_mcp_config`, whose ECA-135 guard would refuse the name before either of
    these writers ran — so it could never exercise them. Ungranted, `_write_mcp_config`
    returns `{}` before validating, the turn runs the full failure ladder, and every
    event plus the failure capsule is written through the hostile name.

    Asserts on the tree OUTSIDE the home rather than on an exception, because the defect
    was a file appearing where it should not.
    """
    hostile = "../../escaped"
    await registry.spawn_worker(  # bypasses Engine.spawn, as a legacy row does
        hostile, str(repo), json.loads(WorkerPolicy().to_json())  # ungranted — see above
    )

    outside_before = {p for p in tmp_path.rglob("*") if cfg.home not in p.parents}
    engine, _ = make_engine([RuntimeError("died"), RuntimeError("died again")])
    engine._ensure_runner(hostile)
    turn = await terminal_turn(registry, await engine.prompt(hostile, "go"))

    assert turn["state"] == "error"  # the ladder ran; the capsule path was reached
    outside_after = {p for p in tmp_path.rglob("*") if cfg.home not in p.parents}
    assert outside_after == outside_before, "a log or capsule escaped the home"
    assert not (tmp_path / "escaped.jsonl").exists()
    assert list(tmp_path.glob("escaped-turn*.json")) == []

    # Nothing was silently dropped: the lane's events were re-keyed to the daemon key,
    # carrying the hostile name in the body as evidence.
    refused = [e for e in events.read("-") if e.get("log_key_refused")]
    assert refused, "the hostile lane's events vanished instead of being re-keyed"
    assert {e["worker"] for e in refused} == {hostile}
    assert any(e["event"] == "failure_capsule_error" for e in refused)


async def test_a_legal_lane_still_gets_its_own_log_and_capsule(
    make_engine, registry, repo, cfg, events
):
    """AC#3 at the engine level: the guard must be invisible to a real lane, and the
    capsule must still land — a refusal that fired on well-formed names would make this
    task a regression rather than a fix."""
    engine, _ = make_engine([RuntimeError("died"), RuntimeError("died again")])
    await engine.spawn("ultra-2", str(repo))
    turn = await terminal_turn(registry, await engine.prompt("ultra-2", "go"))

    assert turn["state"] == "error"
    assert (cfg.logs_dir / "ultra-2.jsonl").exists()
    assert not any(e.get("log_key_refused") for e in events.read("ultra-2"))
    capsules = list(cfg.capsules_dir.glob("ultra-2-turn*.json"))
    assert len(capsules) == 1, f"expected one capsule, got {capsules}"
