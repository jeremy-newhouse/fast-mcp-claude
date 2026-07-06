"""Replay-leg tests (Q4 cred-free model leg). Skipped without the SDK installed;
the pure limits/result/gitcreds tests still gate the commit."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("claude_agent_sdk")

from sandbox_runner import replay as replay_mod  # noqa: E402
from sandbox_runner import runner as runner_mod  # noqa: E402
from sandbox_runner.limits import Limits  # noqa: E402
from sandbox_runner.result import JobRelay, JobState  # noqa: E402


def test_load_spec_flag_returns_default():
    assert replay_mod.load_spec("1") is replay_mod.DEFAULT_SPEC
    assert replay_mod.load_spec("default") is replay_mod.DEFAULT_SPEC


def test_load_spec_file(tmp_path):
    spec = {"messages": [{"type": "result", "subtype": "success"}]}
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    assert replay_mod.load_spec(str(p)) == spec


def test_load_spec_bad_value_raises():
    with pytest.raises(FileNotFoundError):
        replay_mod.load_spec("/no/such/replay/file.json")


async def test_default_replay_runs_completed(tmp_path):
    relay = JobRelay(tmp_path, "r1")
    qfn = replay_mod.make_replay_query_fn(replay_mod.DEFAULT_SPEC)
    out = await runner_mod.run_job(
        prompt="hi", cwd=str(tmp_path), limits=Limits(), relay=relay, query_fn=qfn
    )
    assert out["state"] == JobState.COMPLETED.value
    assert (tmp_path / "result.json").exists()


async def test_replay_budget_subtype_maps(tmp_path):
    relay = JobRelay(tmp_path, "r2")
    spec = {"messages": [{"type": "result", "subtype": "error_max_budget", "is_error": True}]}
    qfn = replay_mod.make_replay_query_fn(spec)
    out = await runner_mod.run_job(
        prompt="hi", cwd=str(tmp_path), limits=Limits(max_budget_usd=0.01), relay=relay, query_fn=qfn
    )
    assert out["state"] == JobState.BUDGET_EXCEEDED.value


async def test_replay_pre_sleep_triggers_wall_clock_timeout(tmp_path):
    relay = JobRelay(tmp_path, "r3")
    spec = {"pre_sleep_s": 5, "messages": [{"type": "result", "subtype": "success"}]}
    qfn = replay_mod.make_replay_query_fn(spec)
    out = await runner_mod.run_job(
        prompt="hi", cwd=str(tmp_path), limits=Limits(wall_clock_s=1), relay=relay, query_fn=qfn
    )
    assert out["state"] == JobState.TIMEOUT.value
