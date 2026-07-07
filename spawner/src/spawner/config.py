"""Spawner configuration (ECA-65). ``pydantic-settings`` loaded from env / ``.env``.

The spawner runs on the operator's peer as the sole NATS client that consumes
``dispatch.<member>.<machine>``. Everything it needs to bind its durable consumer, launch the
hardened container, and mount secrets is here. Member/machine segments follow the same grammar
the hub enforces (``^[a-z0-9-]{1,32}$``) so a bad value can never construct a subject.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SEGMENT_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_RESERVED_SEGMENTS = frozenset({"machine", "team", "hub"})


def validate_segment(value: str, *, kind: str) -> str:
    """Validate a member/machine subject segment (mirrors nats_dispatch.validate_segment)."""
    v = (value or "").strip()
    if not _SEGMENT_RE.match(v):
        raise ValueError(
            f"invalid {kind} id {value!r}: must match ^[a-z0-9-]{{1,32}}$ "
            "(lowercase letters, digits, hyphen; 1-32 chars)"
        )
    if v in _RESERVED_SEGMENTS:
        raise ValueError(f"invalid {kind} id {value!r}: '{v}' is a reserved segment name")
    return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPAWNER_", env_file=".env", extra="ignore"
    )

    # --- Bus ---
    nats_url: str = Field(default="nats://127.0.0.1:4222")
    nats_creds_path: str | None = Field(default=None)
    member_id: str = Field(default="operator")
    machine_id: str = Field(default="mini2")

    # --- Container launch ---
    agent_image: str = Field(default="eca/agent-sandbox:dev")
    docker_bin: str = Field(default="docker")
    egress_network: str = Field(default="eca-egress-internal")
    egress_proxy_name: str = Field(default="eca-egress-proxy")
    seccomp_path: str | None = Field(default=None)
    # Per-job scratch root where request.json / events.jsonl / result.json live (bind-mounted /job).
    job_root: str = Field(default="~/.spawner/jobs")

    # --- Secrets (peer-local store; spawner NEVER mints) ---
    gh_token_path: str | None = Field(default=None)  # 0400 file -> /run/secrets/gh_token
    bedrock_secret_path: str | None = Field(default=None)  # 0400 file -> /run/secrets/bedrock

    # --- State ---
    db_path: str = Field(default="~/.spawner/spawner.db")

    # --- Presence (Q5: 10s heartbeat / 30s staleness) ---
    presence_interval_s: float = Field(default=10.0)

    # --- Payload policy ---
    # Inline cap below the 8MB server max_payload; oversize -> truncate-with-marker (never drop).
    result_inline_cap: int = Field(default=1_000_000)
    event_inline_cap: int = Field(default=1_000_000)

    @field_validator("member_id")
    @classmethod
    def _v_member(cls, v: str) -> str:
        return validate_segment(v, kind="member")

    @field_validator("machine_id")
    @classmethod
    def _v_machine(cls, v: str) -> str:
        return validate_segment(v, kind="machine")

    def path(self, value: str) -> Path:
        """Expand ~ and return an absolute Path."""
        return Path(value).expanduser()

    @property
    def db_file(self) -> Path:
        return self.path(self.db_path)

    @property
    def job_root_dir(self) -> Path:
        return self.path(self.job_root)
