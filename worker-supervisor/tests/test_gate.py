"""AC-WS-2 (code half): off-ceiling / out-of-cwd / guard-hook denies happen in
code with reasons recorded; plus the AskUserQuestion bridge round-trip (FR-WS4)."""

from __future__ import annotations

import asyncio
import json

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from worker_supervisor.gate import QuestionBridge, WorkerPolicy, make_gate


def _gate(repo, policy, bridge, events, timeout=1.0, turn_id=1):
    return make_gate(
        worker="w1",
        repo_root=repo,
        policy=policy,
        bridge=bridge,
        events=events,
        turn_id=turn_id,
        question_timeout_s=timeout,
    )


async def test_off_ceiling_tool_is_denied_with_reason(registry, events, repo):
    bridge = QuestionBridge(registry, events)
    policy = WorkerPolicy(allowed_tools=["Read"])
    gate = _gate(repo, policy, bridge, events)
    res = await gate("WebSearch", {}, None)
    assert isinstance(res, PermissionResultDeny)
    assert "ceiling" in res.message
    assert any(e["event"] == "tool_denied" for e in events.read("w1"))


async def test_bash_prefix_matcher():
    policy = WorkerPolicy(allowed_tools=["Bash(uv run*)"])
    assert policy.ceiling_allows("Bash", {"command": "uv run pytest -q"})
    assert not policy.ceiling_allows("Bash", {"command": "rm -rf /"})


async def test_cwd_pin_denies_escape_and_allows_inside(registry, events, repo, tmp_path):
    bridge = QuestionBridge(registry, events)
    policy = WorkerPolicy(allowed_tools=["Read"])
    gate = _gate(repo, policy, bridge, events)
    inside = await gate("Read", {"file_path": str(repo / "a.txt")}, None)
    assert isinstance(inside, PermissionResultAllow)
    outside = await gate("Read", {"file_path": str(tmp_path / "outside.txt")}, None)
    assert isinstance(outside, PermissionResultDeny)
    assert "escapes" in outside.message


async def test_cwd_pin_catches_symlink_escape(registry, events, repo, tmp_path):
    target = tmp_path / "secret"
    target.mkdir()
    (repo / "link").symlink_to(target)
    bridge = QuestionBridge(registry, events)
    gate = _gate(repo, WorkerPolicy(allowed_tools=["Read"]), bridge, events)
    res = await gate("Read", {"file_path": str(repo / "link" / "x.txt")}, None)
    assert isinstance(res, PermissionResultDeny)


async def test_guard_hook_deny_is_honored(registry, events, repo):
    hooks_dir = repo / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    guard = hooks_dir / "no-writes.sh"
    guard.write_text(
        "#!/usr/bin/env bash\n"
        'echo \'{"hookSpecificOutput": {"permissionDecision": "deny",'
        ' "message": "writes are frozen"}}\'\n'
    )
    bridge = QuestionBridge(registry, events)
    policy = WorkerPolicy(allowed_tools=["Write"], guard_hooks={"Write": "no-writes.sh"})
    gate = _gate(repo, policy, bridge, events)
    res = await gate("Write", {"file_path": str(repo / "f.txt")}, None)
    assert isinstance(res, PermissionResultDeny)
    assert "writes are frozen" in res.message


async def test_question_bridge_round_trip(registry, events, repo):
    await registry.spawn_worker("w1", str(repo), {})
    tid = await registry.enqueue_turn("w1", "asking turn")
    bridge = QuestionBridge(registry, events)
    gate = _gate(repo, WorkerPolicy(), bridge, events, timeout=5.0, turn_id=tid)
    payload = {"questions": [{"question": "Deploy now?", "options": ["yes", "no"]}]}

    ask_task = asyncio.create_task(gate("AskUserQuestion", payload, None))
    # wait for it to park, then answer over the "control surface"
    for _ in range(50):
        pending = await registry.pending_questions("w1")
        if pending:
            break
        await asyncio.sleep(0.02)
    assert pending, "question never parked"
    assert json.loads(pending[0]["questions"]) == payload["questions"]
    assert await bridge.answer(pending[0]["id"], "yes — ship it")

    res = await ask_task
    assert isinstance(res, PermissionResultDeny)
    assert res.message == "The user responded: yes — ship it"
    assert not res.interrupt


async def test_question_timeout_interrupts_turn(registry, events, repo):
    await registry.spawn_worker("w1", str(repo), {})
    tid = await registry.enqueue_turn("w1", "asking turn")
    bridge = QuestionBridge(registry, events)
    gate = _gate(repo, WorkerPolicy(), bridge, events, timeout=0.05, turn_id=tid)
    res = await gate("AskUserQuestion", {"questions": [{"question": "?"}]}, None)
    assert isinstance(res, PermissionResultDeny)
    assert res.interrupt is True
    qs = await registry.db.execute("SELECT state FROM questions")
    states = [r["state"] for r in await qs.fetchall()]
    assert states == ["timed_out"]


def test_base_tools_always_include_escalation_and_skills():
    policy = WorkerPolicy(allowed_tools=["Read", "Bash(uv run*)"])
    base = policy.base_tools()
    assert "AskUserQuestion" in base and "Skill" in base
    assert "Bash" in base and "Read" in base
