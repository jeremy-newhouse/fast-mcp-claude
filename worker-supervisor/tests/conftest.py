from __future__ import annotations

import pytest

from worker_supervisor.config import Config, Limits
from worker_supervisor.events import EventLog
from worker_supervisor.registry import Registry


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        home=tmp_path / "home",
        limits=Limits(wall_clock_s=5, max_turns=5, max_budget_usd_per_epoch=1.0),
        question_timeout_s=1,
        cycle_context_pct=80,
        max_concurrent_turns=2,
        idle_timeout_s=3600,
        mcp_startup_grace_s=0.0,
        mesh_url=None,
        mesh_api_key=None,
        machine="testhost",
        announce_interval_s=60,
    )


@pytest.fixture
async def registry(cfg) -> Registry:
    reg = Registry(cfg.db_path)
    await reg.connect()
    yield reg
    await reg.close()


@pytest.fixture
def events(cfg) -> EventLog:
    return EventLog(cfg.logs_dir)


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    return d
