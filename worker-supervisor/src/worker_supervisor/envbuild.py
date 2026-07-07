"""Allowlist-built worker environments (FR-WS10 / AC-WS-9, Amendment A7).

SDK fact this design rests on (claude-agent-sdk 0.2.91, subprocess_cli.py:425-482):
`ClaudeAgentOptions.env` MERGES onto the parent's full `os.environ` — it cannot
remove variables. The structural guarantee therefore has two halves:

1. `scrub_daemon_env()` — at boot, AFTER config load, the daemon's own
   `os.environ` is reduced to the minimal base. Anything not in the base
   (API keys, Bedrock/AWS credentials, mesh bearers, ...) is gone from the
   parent and can never be inherited by any worker subprocess.
2. `build_worker_env()` — per-worker additions are an explicit allowlist
   resolved against the boot-time snapshot; credential-class names are
   rejected outright, so a spawn can't opt a worker into API billing.

Model auth stays the host's logged-in Claude CLI subscription (A3/G11).
"""

from __future__ import annotations

import os

# What a worker subprocess legitimately needs from the host.
MINIMAL_BASE = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)

# Never forwardable, not even via --allow-env (A3: subscription auth only).
FORBIDDEN = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
    "MCP_API_KEY",
    "MESH_API_KEY",
)


def snapshot_boot_env() -> dict[str, str]:
    """Capture the daemon's original environment before the scrub."""
    return dict(os.environ)


def scrub_daemon_env(snapshot: dict[str, str] | None = None) -> dict[str, str]:
    """Reduce the daemon's own os.environ to the minimal base. Returns what was kept.

    Call once at boot, after load_config() has pulled everything it needs into
    memory. From this point the parent env — which every SDK subprocess inherits
    wholesale — contains nothing a worker shouldn't see.
    """
    src = snapshot if snapshot is not None else dict(os.environ)
    kept = {k: src[k] for k in MINIMAL_BASE if k in src}
    os.environ.clear()
    os.environ.update(kept)
    return kept


def validate_allow_env(names: list[str]) -> None:
    bad = sorted(set(n for n in names if n in FORBIDDEN))
    if bad:
        raise ValueError(f"allow_env rejects credential-class names: {', '.join(bad)}")


def build_worker_env(
    snapshot: dict[str, str],
    allow_env: list[str],
    *,
    mcp_tool_timeout_ms: int,
) -> dict[str, str]:
    """The per-worker ClaudeAgentOptions.env dict: explicit opt-ins only.

    The minimal base is already the parent env (post-scrub) and is inherited;
    this dict carries only the spawn-time allowlist plus supervisor-set knobs.
    """
    validate_allow_env(allow_env)
    env = {name: snapshot[name] for name in allow_env if name in snapshot}
    # G6: must exceed the longest in-tool block or the CLI kills the call —
    # AskUserQuestion parks inside can_use_tool for up to the question timeout.
    env["MCP_TOOL_TIMEOUT"] = str(mcp_tool_timeout_ms)
    return env
