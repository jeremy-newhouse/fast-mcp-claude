"""Runner tests with a stubbed model leg (Q4: cred-free, CI-safe).

``runner`` imports ``claude_agent_sdk`` at module load, so if the SDK is not
installed locally these are skipped — the pure limits/result/gitcreds tests
still gate the commit. In the image (SDK pinned) they run for real.
"""

from __future__ import annotations

import pytest

sdk = pytest.importorskip("claude_agent_sdk")

from sandbox_runner import runner as runner_mod  # noqa: E402
from sandbox_runner.limits import Limits  # noqa: E402
from sandbox_runner.result import JobRelay, JobState  # noqa: E402


def _make_stub(messages):
    async def _stub(*, prompt, options):  # matches query(prompt=, options=)
        # Drain the prompt stream so the input contract is exercised too.
        async for _ in prompt:
            pass
        for m in messages:
            yield m
    return _stub


def _result_msg(*, subtype="success", cost=0.01, turns=1, text="ok", is_error=False):
    return sdk.ResultMessage(
        subtype=subtype,
        duration_ms=5,
        duration_api_ms=4,
        is_error=is_error,
        num_turns=turns,
        session_id="s1",
        total_cost_usd=cost,
        usage={"input_tokens": 3},
        result=text,
    )


async def test_completed_job_writes_result(tmp_path):
    relay = JobRelay(tmp_path, "j1")
    assistant = sdk.AssistantMessage(content=[sdk.TextBlock(text="hello")], model="m")
    out = await runner_mod.run_job(
        prompt="hi",
        cwd=str(tmp_path),
        limits=Limits(max_turns=1, max_budget_usd=1.0, wall_clock_s=30),
        relay=relay,
        model="test-model",
        query_fn=_make_stub([assistant, _result_msg()]),
    )
    assert out["state"] == JobState.COMPLETED.value
    assert out["total_cost_usd"] == 0.01
    assert out["final_text"] == "hello"
    assert (tmp_path / "result.json").exists()


async def test_budget_subtype_maps_to_budget_exceeded(tmp_path):
    relay = JobRelay(tmp_path, "j2")
    out = await runner_mod.run_job(
        prompt="hi", cwd=str(tmp_path),
        limits=Limits(max_budget_usd=0.01), relay=relay,
        query_fn=_make_stub([_result_msg(subtype="error_max_budget", is_error=True)]),
    )
    assert out["state"] == JobState.BUDGET_EXCEEDED.value


async def test_max_turns_subtype_maps_to_turn_limit(tmp_path):
    relay = JobRelay(tmp_path, "j3")
    out = await runner_mod.run_job(
        prompt="hi", cwd=str(tmp_path),
        limits=Limits(max_turns=1), relay=relay,
        query_fn=_make_stub([_result_msg(subtype="error_max_turns", is_error=True)]),
    )
    assert out["state"] == JobState.TURN_LIMIT.value


async def test_no_result_message_is_error(tmp_path):
    relay = JobRelay(tmp_path, "j4")
    out = await runner_mod.run_job(
        prompt="hi", cwd=str(tmp_path), limits=Limits(), relay=relay,
        query_fn=_make_stub([]),
    )
    assert out["state"] == JobState.ERROR.value
    assert "ResultMessage" in out["error"]


async def test_wall_clock_breach_is_timeout(tmp_path):
    import asyncio

    async def _slow(*, prompt, options):
        async for _ in prompt:
            pass
        await asyncio.sleep(5)
        yield _result_msg()

    relay = JobRelay(tmp_path, "j5")
    out = await runner_mod.run_job(
        prompt="hi", cwd=str(tmp_path),
        limits=Limits(wall_clock_s=1), relay=relay, query_fn=_slow,
    )
    assert out["state"] == JobState.TIMEOUT.value
