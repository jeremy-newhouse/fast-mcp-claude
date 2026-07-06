"""In-container SDK runner (AC#2): drive ONE hermetic `claude_agent_sdk` session
per container invocation, under the limits triple, relaying to the job dir.

Hermetic posture (the container *is* the boundary — container-sandbox.md):
  * ``permission_mode='bypassPermissions'`` — no in-container gate; the sandbox
    (cap-drop, read-only rootfs, seccomp, egress proxy) is the control surface.
  * ``setting_sources=[]`` — ignore any on-disk settings; no ambient config.
  * no MCP servers (invariant-9 analog) — leaner than worker-supervisor.
  * ``disallowed_tools=[WebFetch, WebSearch]`` — kills general web + the
    ``api.anthropic.com`` preflight; only git + Bedrock egress is allowed anyway.
  * model pinned from the request / ``ANTHROPIC_MODEL`` (Bedrock default is
    Sonnet — must pin to our posture).

Limits: ``max_turns`` + ``max_budget_usd`` are native SDK options (both exist in
0.2.91); ``wall_clock_s`` is enforced here via ``asyncio.timeout`` — a breach
cancels the transport, escalating SIGTERM->SIGKILL to the CLI subprocess group.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from .limits import Limits
from .result import JobRelay, JobState, build_result

# Tools that must never run in the sandbox: no general web egress is allowed and
# WebFetch/WebSearch also trigger an api.anthropic.com preflight we don't permit.
DISALLOWED_TOOLS = ["WebFetch", "WebSearch"]

# SDK ResultMessage.subtype values that mean a limit bit, mapped to our states.
_SUBTYPE_STATE = {
    "success": JobState.COMPLETED,
    "error_max_turns": JobState.TURN_LIMIT,
    "error_max_budget": JobState.BUDGET_EXCEEDED,
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


async def _prompt_as_stream(prompt: str) -> AsyncIterator[dict[str, Any]]:
    """Single-message streaming input (matches the launcher/worker-supervisor shape)."""
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
    }


def build_options(
    *,
    cwd: str,
    limits: Limits,
    model: str | None,
    env: dict[str, str] | None = None,
) -> ClaudeAgentOptions:
    """Assemble the hermetic per-job SDK options."""
    return ClaudeAgentOptions(
        cwd=cwd,
        setting_sources=[],
        permission_mode="bypassPermissions",
        disallowed_tools=DISALLOWED_TOOLS,
        max_turns=limits.max_turns,
        max_budget_usd=limits.sdk_budget_usd,
        model=model,
        env=env or {},
    )


class _Outcome:
    """Mutable accumulator populated while observing the message stream."""

    __slots__ = ("total_cost_usd", "num_turns", "usage", "final_text", "state", "error")

    def __init__(self) -> None:
        self.total_cost_usd: float | None = None
        self.num_turns: int | None = None
        self.usage: dict[str, Any] | None = None
        self.final_text: str | None = None
        self.state: JobState | None = None
        self.error: str | None = None


def _observe(msg: Any, outcome: _Outcome, relay: JobRelay) -> None:
    """Fold one SDK message into the outcome + emit a live event frame."""
    if isinstance(msg, AssistantMessage):
        texts: list[str] = []
        tools: list[str] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                texts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tools.append(block.name)
        if texts:
            outcome.final_text = "\n".join(texts)
            relay.emit("assistant", text_len=sum(len(t) for t in texts))
        for name in tools:
            relay.emit("tool_use", tool=name)
    elif isinstance(msg, ResultMessage):
        outcome.total_cost_usd = getattr(msg, "total_cost_usd", None)
        outcome.num_turns = getattr(msg, "num_turns", None)
        if getattr(msg, "usage", None):
            outcome.usage = msg.usage
        subtype = getattr(msg, "subtype", None)
        outcome.state = _SUBTYPE_STATE.get(subtype or "", JobState.COMPLETED)
        if getattr(msg, "is_error", False) and outcome.state is JobState.COMPLETED:
            outcome.state = JobState.ERROR
            outcome.error = getattr(msg, "result", None) or f"result error ({subtype})"
        relay.emit(
            "result",
            subtype=subtype,
            total_cost_usd=outcome.total_cost_usd,
            num_turns=outcome.num_turns,
        )


async def run_job(
    *,
    prompt: str,
    cwd: str,
    limits: Limits,
    relay: JobRelay,
    model: str | None = None,
    env: dict[str, str] | None = None,
    query_fn: Any = query,
) -> dict[str, Any]:
    """Run one SDK session to completion (or a limit breach) and finalize the result.

    ``query_fn`` defaults to the live SDK ``query``; smoke/tests inject a
    stub/replay (Q4: cred-free, CI-safe model leg) with the same async-iterator
    signature. Returns the terminal result dict (also written to ``result.json``).
    """
    import asyncio

    started_at = _utcnow_iso()
    t0 = time.monotonic()
    options = build_options(cwd=cwd, limits=limits, model=model, env=env)
    outcome = _Outcome()
    relay.emit(
        "lifecycle",
        phase="query_start",
        model=model,
        limits=limits.as_dict(),
    )

    try:
        async with asyncio.timeout(limits.wall_clock_s):
            async for msg in query_fn(prompt=_prompt_as_stream(prompt), options=options):
                _observe(msg, outcome, relay)
    except (asyncio.TimeoutError, TimeoutError):
        outcome.state = JobState.TIMEOUT
        outcome.error = f"wall clock exceeded ({limits.wall_clock_s}s)"
        relay.emit("lifecycle", phase="timeout", wall_clock_s=limits.wall_clock_s)
    except Exception as exc:  # noqa: BLE001 — any SDK/transport failure is a job error
        outcome.state = JobState.ERROR
        outcome.error = f"{type(exc).__name__}: {exc}"
        relay.emit("lifecycle", phase="error", error=outcome.error)

    if outcome.state is None:
        # Stream ended with no ResultMessage — treat as an error, not a success.
        outcome.state = JobState.ERROR
        outcome.error = outcome.error or "stream ended without a ResultMessage"

    duration_ms = int((time.monotonic() - t0) * 1000)
    result = build_result(
        state=outcome.state,
        total_cost_usd=outcome.total_cost_usd,
        num_turns=outcome.num_turns,
        usage=outcome.usage,
        final_text=outcome.final_text,
        started_at=started_at,
        duration_ms=duration_ms,
        error=outcome.error,
    )
    relay.finalize(result)
    relay.emit("lifecycle", phase="finalized", state=outcome.state.value)
    return result


def runner_env_from_process() -> dict[str, str]:
    """The scoped env handed to the SDK/CLI subprocess.

    The Bedrock bearer is injected into THIS process's env by entrypoint.sh (never
    container-wide). We forward the model + Bedrock belt explicitly rather than the
    whole environ so no unrelated host var leaks into the CLI.
    """
    keep = (
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_AUTOUPDATER",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "HOME",
        "PATH",
    )
    return {k: os.environ[k] for k in keep if k in os.environ}
