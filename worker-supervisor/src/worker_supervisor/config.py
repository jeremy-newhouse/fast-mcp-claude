"""Configuration: .env-loaded defaults; per-worker spawn overrides win.

Keys and defaults are the design of record's table
(evolv-coder-agent docs/architecture/worker-supervisor.md #configuration).
Real env always wins over .env (dotenv never overrides existing vars).
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

# tools/worker-supervisor/.env when running from the project; overridable.
_PROJECT_ENV = Path(__file__).resolve().parents[2] / ".env"


@dataclass(frozen=True)
class Limits:
    """The per-turn/per-epoch limit triple (name-compatible with the spawner)."""

    wall_clock_s: int = 1800
    max_turns: int = 50
    max_budget_usd_per_epoch: float = 10.0

    def override(self, spec: dict | None) -> "Limits":
        """Apply per-worker spawn overrides (unknown keys rejected upstream)."""
        if not spec:
            return self
        return replace(
            self,
            **{k: v for k, v in spec.items() if k in ("wall_clock_s", "max_turns")},
            **(
                {"max_budget_usd_per_epoch": float(spec["max_budget_usd_per_epoch"])}
                if "max_budget_usd_per_epoch" in spec
                else {}
            ),
        )


@dataclass(frozen=True)
class Config:
    home: Path
    limits: Limits
    question_timeout_s: int
    cycle_context_pct: int
    max_concurrent_turns: int
    idle_timeout_s: int
    # ECA-101: one-shot query()'s wait_for_result_and_end_input() only waits on
    # 'sdk'-type (in-process) mcp_servers before delivering the turn's prompt —
    # never the stdio/http/https servers a worker policy actually grants. This
    # grace gives them a head start connecting before the prompt lands.
    mcp_startup_grace_s: float
    # mesh presence (Amendment A4); disabled when url or key is unset
    mesh_url: str | None
    mesh_api_key: str | None
    machine: str
    announce_interval_s: int
    # AF_UNIX paths cap at ~104 bytes on macOS — a deep SUPERVISOR_HOME needs a
    # short socket override (SUPERVISOR_SOCKET). Captured at load time: the
    # daemon scrubs its env after boot, so an env-reading property would drift.
    socket_override: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.home / "state.db"

    @property
    def socket_path(self) -> Path:
        return self.socket_override or self.home / "supervisor.sock"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def capsules_dir(self) -> Path:
        return self.home / "capsules"


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, "") or default)


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, "") or default)


def load_config(env_file: str | Path | None = None) -> Config:
    load_dotenv(env_file or os.environ.get("SUPERVISOR_ENV_FILE") or _PROJECT_ENV)

    home = Path(os.environ.get("SUPERVISOR_HOME", "~/.worker-supervisor")).expanduser()
    machine = os.environ.get("SUPERVISOR_MACHINE") or socket.gethostname().split(".")[0]
    return Config(
        home=home,
        limits=Limits(
            wall_clock_s=_i("SUPERVISOR_MAX_WALL_CLOCK_S", 1800),
            max_turns=_i("SUPERVISOR_MAX_TURNS", 50),
            max_budget_usd_per_epoch=_f("SUPERVISOR_MAX_BUDGET_USD_PER_EPOCH", 10.0),
        ),
        question_timeout_s=_i("SUPERVISOR_QUESTION_TIMEOUT_S", 14400),
        cycle_context_pct=_i("SUPERVISOR_CYCLE_CONTEXT_PCT", 80),
        max_concurrent_turns=_i("SUPERVISOR_MAX_CONCURRENT_TURNS", 4),
        idle_timeout_s=_i("SUPERVISOR_WORKER_IDLE_TIMEOUT_S", 86400),
        mcp_startup_grace_s=_f("SUPERVISOR_MCP_STARTUP_GRACE_S", 3.0),
        mesh_url=os.environ.get("MESH_URL") or None,
        mesh_api_key=os.environ.get("MESH_API_KEY") or None,
        machine=machine,
        announce_interval_s=_i("SUPERVISOR_ANNOUNCE_INTERVAL_S", 60),
        socket_override=(
            Path(os.environ["SUPERVISOR_SOCKET"]).expanduser()
            if os.environ.get("SUPERVISOR_SOCKET")
            else None
        ),
    )
