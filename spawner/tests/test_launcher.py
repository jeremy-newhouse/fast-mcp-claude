"""Unit tests for DockerLauncher argv construction (the cred-free replay knob, ECA-65).

The launcher is tested through the ``docker run`` argv it builds — the same "logic in a testable
method" split the rest of the fleet uses. Here we pin the ``SANDBOX_RUNNER_REPLAY`` leg: present
as a container-wide ``-e`` iff ``agent_replay`` is on, and absent otherwise, without pulling in a
Bedrock secret bind-mount.
"""

from __future__ import annotations

from pathlib import Path

from spawner.config import Settings
from spawner.launcher import DockerLauncher


def _argv(**overrides: object) -> list[str]:
    settings = Settings(**overrides)  # type: ignore[arg-type]
    return DockerLauncher(settings)._run_argv("job-1", Path("/tmp/jobs/job-1"))


def _has_pair(argv: list[str], flag: str, value: str) -> bool:
    """True iff ``flag`` is immediately followed by ``value`` (a real docker -e pair)."""
    return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))


def test_replay_env_present_when_knob_on():
    argv = _argv(agent_replay=True)
    assert _has_pair(argv, "-e", "SANDBOX_RUNNER_REPLAY=1")


def test_replay_env_absent_by_default():
    argv = _argv()
    assert "SANDBOX_RUNNER_REPLAY=1" not in argv


def test_replay_env_absent_when_knob_off():
    argv = _argv(agent_replay=False)
    assert "SANDBOX_RUNNER_REPLAY=1" not in argv


def test_replay_mode_needs_no_bedrock_secret_mount():
    # Cred-free e2e: replay on and no bedrock secret configured -> no bedrock bind-mount at all.
    argv = _argv(agent_replay=True, bedrock_secret_path=None)
    assert _has_pair(argv, "-e", "SANDBOX_RUNNER_REPLAY=1")
    assert not any("/run/secrets/bedrock" in arg for arg in argv)
