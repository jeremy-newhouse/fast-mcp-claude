"""AC-WS-9: worker env is allowlist-built; credential vars structurally excluded."""

from __future__ import annotations

import os

import pytest

from worker_supervisor.envbuild import (
    FORBIDDEN,
    MINIMAL_BASE,
    build_worker_env,
    scrub_daemon_env,
    validate_allow_env,
)

SNAPSHOT = {
    "PATH": "/usr/bin",
    "HOME": "/Users/op",
    "ANTHROPIC_API_KEY": "sk-live-SECRET",
    "AWS_SECRET_ACCESS_KEY": "aws-SECRET",
    "MESH_API_KEY": "bearer-SECRET",
    "MY_TOOL_FLAG": "1",
    "OTHER_VAR": "x",
}


def test_built_env_contains_only_allowed_names_plus_timeout_and_path():
    env = build_worker_env(SNAPSHOT, ["MY_TOOL_FLAG"], mcp_tool_timeout_ms=1000)
    # SNAPSHOT PATH is "/usr/bin", so the standard macOS dirs are prepended.
    assert env == {
        "MY_TOOL_FLAG": "1",
        "MCP_TOOL_TIMEOUT": "1000",
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin",
    }


def test_path_not_augmented_when_dirs_already_present():
    snap = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin"}
    env = build_worker_env(snap, [], mcp_tool_timeout_ms=1000)
    assert "PATH" not in env  # already covered -> no override emitted


def test_path_augmented_preserves_existing_when_dir_missing():
    snap = {"PATH": "/opt/homebrew/bin:/usr/bin"}  # only /usr/local/bin missing
    env = build_worker_env(snap, [], mcp_tool_timeout_ms=1000)
    assert env["PATH"] == "/usr/local/bin:/opt/homebrew/bin:/usr/bin"


def test_credential_vars_never_reach_a_worker_even_when_present():
    env = build_worker_env(SNAPSHOT, [], mcp_tool_timeout_ms=1000)
    for name in FORBIDDEN:
        assert name not in env


def test_allow_env_rejects_credential_class_names():
    for name in ("ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "MESH_API_KEY"):
        with pytest.raises(ValueError, match="credential-class"):
            validate_allow_env([name])


def test_scrub_daemon_env_reduces_to_minimal_base():
    """The SDK merges options.env onto the parent env — the scrub IS the guarantee."""
    original = dict(os.environ)
    try:
        os.environ.update(SNAPSHOT)
        kept = scrub_daemon_env()
        assert set(kept) <= set(MINIMAL_BASE)
        assert "ANTHROPIC_API_KEY" not in os.environ
        assert "AWS_SECRET_ACCESS_KEY" not in os.environ
        assert "MESH_API_KEY" not in os.environ
        assert "MY_TOOL_FLAG" not in os.environ  # not in base -> gone
        assert os.environ["PATH"] == "/usr/bin"  # base survives
    finally:
        os.environ.clear()
        os.environ.update(original)
