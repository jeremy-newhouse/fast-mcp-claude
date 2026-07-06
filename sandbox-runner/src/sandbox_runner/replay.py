"""Cred-free replay model leg (Q4): a ``query_fn`` that yields canned SDK messages
instead of calling the live Bedrock-backed model.

This is the seam that lets the smoke test (``../smoke/``) exercise the WHOLE
container path — clone via credential helper, the hermetic session plumbing, the
job-dir relay, the ``result.json`` contract, wall-clock enforcement — WITHOUT a
Bedrock bearer, so AC#5's clone/limits/egress/layer legs run live and CI-safe.
The live model leg stays the default (`__main__` only swaps in replay when
``SANDBOX_RUNNER_REPLAY`` is set); an operator with a real bearer runs it live.

Selection (env, read by ``__main__``):
  * ``SANDBOX_RUNNER_REPLAY=1`` (or ``default``) — a built-in one-turn success.
  * ``SANDBOX_RUNNER_REPLAY=/path/to/spec.json`` — a canned script:

        {
          "pre_sleep_s": 0,            # sleep before yielding (exercise wall-clock)
          "messages": [
            {"type": "assistant", "text": "..."},
            {"type": "result", "subtype": "success",
             "total_cost_usd": 0.01, "num_turns": 1,
             "usage": {"input_tokens": 10}, "is_error": false, "result": "done"}
          ]
        }

``subtype`` drives the outcome the runner maps: ``error_max_budget`` ->
``budget_exceeded``, ``error_max_turns`` -> ``turn_limit`` (so the smoke can prove
the limit->state plumbing end-to-end through the container; native SDK enforcement
of those two is covered by the unit tests + the optional live leg).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

DEFAULT_SPEC: dict[str, Any] = {
    "pre_sleep_s": 0,
    "messages": [
        {"type": "assistant", "text": "Replay leg: no live model was called."},
        {
            "type": "result",
            "subtype": "success",
            "total_cost_usd": 0.0,
            "num_turns": 1,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "is_error": False,
            "result": "replay-ok",
        },
    ],
}


def load_spec(value: str) -> dict[str, Any]:
    """Resolve the ``SANDBOX_RUNNER_REPLAY`` env value to a replay spec dict."""
    if value in ("1", "default", "true", "yes"):
        return DEFAULT_SPEC
    path = Path(value)
    if path.is_file():
        spec = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError("replay spec file must be a JSON object")
        return spec
    raise FileNotFoundError(f"SANDBOX_RUNNER_REPLAY={value!r} is neither a flag nor a file")


def _build_messages(spec: dict[str, Any]) -> list[Any]:
    """Turn the spec's message dicts into real SDK message objects."""
    # Imported here so the module is importable without the SDK (pure-unit CI);
    # in the image the SDK is pinned and present.
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    built: list[Any] = []
    for m in spec.get("messages", []):
        kind = m.get("type")
        if kind == "assistant":
            built.append(
                AssistantMessage(
                    content=[TextBlock(text=m.get("text", ""))],
                    model=m.get("model", "replay"),
                )
            )
        elif kind == "result":
            built.append(
                ResultMessage(
                    subtype=m.get("subtype", "success"),
                    duration_ms=m.get("duration_ms", 1),
                    duration_api_ms=m.get("duration_api_ms", 1),
                    is_error=bool(m.get("is_error", False)),
                    num_turns=m.get("num_turns", 1),
                    session_id=m.get("session_id", "replay"),
                    total_cost_usd=m.get("total_cost_usd", 0.0),
                    usage=m.get("usage", {}),
                    result=m.get("result", ""),
                )
            )
        else:
            raise ValueError(f"unknown replay message type: {kind!r}")
    return built


def make_replay_query_fn(spec: dict[str, Any]) -> Any:
    """Return an async ``query_fn`` (signature ``query(prompt=, options=)``)."""
    pre_sleep_s = float(spec.get("pre_sleep_s", 0) or 0)
    messages = _build_messages(spec)

    async def _replay(*, prompt: Any, options: Any) -> Any:
        # Drain the prompt stream so the input contract is exercised too.
        async for _ in prompt:
            pass
        if pre_sleep_s > 0:
            await asyncio.sleep(pre_sleep_s)
        for msg in messages:
            yield msg

    return _replay


def load_replay_query_fn(value: str) -> Any:
    """Convenience: env value -> spec -> query_fn."""
    return make_replay_query_fn(load_spec(value))
