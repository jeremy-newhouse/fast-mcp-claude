"""Shared pytest fixtures."""

from pathlib import Path

import pytest

from fast_mcp_claude.config import Settings
from fast_mcp_claude.services.store import Store


@pytest.fixture
def settings_factory(tmp_path: Path):
    """Build a Settings instance pointed at a temp dir, with arbitrary overrides."""

    def _factory(**overrides) -> Settings:
        defaults = {
            "peer_name": "test-peer",
            "mcp_host": "127.0.0.1",
            "mcp_port": 5499,
            "mcp_api_key": None,
            "mcp_auth_enabled": False,
            "peers": [],
            "workspace_roots": "",
            "db_path": str(tmp_path / "store.db"),
            "store_ttl_seconds": 3600,
            "poll_max_wait_s": 5,
            "poll_heartbeat_s": 2,
            "log_level": "WARNING",
            "log_format": "console",
        }
        defaults.update(overrides)
        return Settings(**defaults)

    return _factory


@pytest.fixture
async def store(settings_factory):
    s = Store(settings_factory())
    await s.initialize()
    try:
        yield s
    finally:
        await s.close()
