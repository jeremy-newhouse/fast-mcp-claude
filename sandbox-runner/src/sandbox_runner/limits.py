"""The per-job limit triple {wall_clock_s, max_turns, max_budget_usd} (AC#2).

Name-compatible with the spawner (ECA-65) and worker-supervisor's `Limits`.

Enforcement split (confirmed against the pinned SDK, not assumed):
  * ``max_turns``       — native ``ClaudeAgentOptions.max_turns``.
  * ``max_budget_usd``  — native ``ClaudeAgentOptions.max_budget_usd`` (this
    option DOES exist in claude-agent-sdk 0.2.91; worker-supervisor uses it).
    The runner also keeps a defensive running-cost tally purely for reporting.
  * ``wall_clock_s``    — runner-enforced via ``asyncio.timeout`` around the
    query stream; on breach the transport is cancelled (SIGTERM->SIGKILL to the
    CLI subprocess group) and the terminal state is ``timeout``. Not derived
    from any token TTL — an independent bound.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

# Defaults mirror worker-supervisor's Limits so a job with no overrides behaves
# identically to a locally-supervised turn.
DEFAULT_WALL_CLOCK_S = 1800
DEFAULT_MAX_TURNS = 50
DEFAULT_MAX_BUDGET_USD = 10.0

# Floor handed to the SDK: max_budget_usd must be > 0 or the option is a no-op.
MIN_BUDGET_USD = 0.01

_FIELDS = ("wall_clock_s", "max_turns", "max_budget_usd")


@dataclass(frozen=True)
class Limits:
    """Immutable limit triple. Construct via :meth:`from_spec` from request JSON."""

    wall_clock_s: int = DEFAULT_WALL_CLOCK_S
    max_turns: int = DEFAULT_MAX_TURNS
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD

    def __post_init__(self) -> None:
        if self.wall_clock_s <= 0:
            raise ValueError(f"wall_clock_s must be positive, got {self.wall_clock_s}")
        if self.max_turns <= 0:
            raise ValueError(f"max_turns must be positive, got {self.max_turns}")
        if self.max_budget_usd <= 0:
            raise ValueError(f"max_budget_usd must be positive, got {self.max_budget_usd}")

    @classmethod
    def from_spec(cls, spec: dict[str, Any] | None) -> "Limits":
        """Build from an untrusted request dict; unknown keys ignored, types coerced."""
        base = cls()
        if not spec:
            return base
        return replace(
            base,
            **{
                "wall_clock_s": int(spec["wall_clock_s"])
                if "wall_clock_s" in spec and spec["wall_clock_s"] is not None
                else base.wall_clock_s,
                "max_turns": int(spec["max_turns"])
                if "max_turns" in spec and spec["max_turns"] is not None
                else base.max_turns,
                "max_budget_usd": float(spec["max_budget_usd"])
                if "max_budget_usd" in spec and spec["max_budget_usd"] is not None
                else base.max_budget_usd,
            },
        )

    @property
    def sdk_budget_usd(self) -> float:
        """The value to hand ``ClaudeAgentOptions.max_budget_usd`` (floored, rounded)."""
        return max(MIN_BUDGET_USD, round(self.max_budget_usd, 4))

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in _FIELDS}
