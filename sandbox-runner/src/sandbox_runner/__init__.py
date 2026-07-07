"""sandbox_runner — the in-container SDK runner for the hardened Docker agent
sandbox (ECA-64).

One job per container invocation: (optionally) clone a repo via a credential
helper, drive a single `claude_agent_sdk` session under the limits triple
{wall_clock_s, max_turns, max_budget_usd}, and relay progress to a bind-mounted
job directory (`events.jsonl` appended live; `result.json` written last via
atomic rename). No NATS, no MCP, no host creds baked in — the container is the
security boundary; the spawner (ECA-65) owns launch flags and result publication.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "0.1.0"
