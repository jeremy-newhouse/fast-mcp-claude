"""Configuration via pydantic-settings (loads from .env / environment).

This server has no domain-specific config (no JIRA, no GitHub, etc.) — instead
it ships with:
  - A peer registry (PEERS env var, JSON) for outbound calls to other servers
  - A workspace allowlist (WORKSPACE_ROOTS) for the file-bridge sandbox
  - Standard auth/host/port/log settings
"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class PeerConfig(BaseModel):
    """A remote peer this server can call out to.

    `url` should include the full MCP path, e.g. https://host:5473/mcp
    `api_key` is the REMOTE peer's MCP_API_KEY (the bearer this server sends).
    """

    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    url: str
    api_key: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("Peer url must start with http:// or https://")
        return v.rstrip("/")


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Identity of THIS machine (sent as `sender` on outbound messages)
    peer_name: str = "local"

    # Server bind config
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 5473  # one above fast-mcp-jira (5472)

    # MCP endpoint authentication (bearer token clients must send to us)
    mcp_api_key: str | None = None
    mcp_auth_enabled: bool = True

    # Peer registry — JSON list of PeerConfig
    peers: list[PeerConfig] = Field(default_factory=list)

    # File-bridge sandbox — colon-separated allowlist of directory paths.
    # Use a string here (not list[Path]) because pydantic-settings parses
    # list[str] from env vars by splitting on commas, which conflicts with
    # path syntax. We accept colon-separated like $PATH for clarity.
    workspace_roots: str = ""

    # Persistent storage
    db_path: str = "~/.fast-mcp-claude/store.db"
    store_ttl_seconds: int = 604800  # 7 days

    # Long-poll tuning. Defaults stay BELOW Claude Code's MCP idle timeout
    # (~30s for stdio; longer for streamable-http). Worker loops should call
    # wait_for_instruction repeatedly so the connection stays warm.
    poll_max_wait_s: int = 25
    poll_heartbeat_s: int = 20

    # Channel adapter (fast-mcp-claude-channel) — STRICT opt-in. Default off so a
    # configured-but-unintended adapter stays inert (handshake only) and never
    # claims inbox messages out from under /worker loop mode. Arming the push
    # bridge requires BOTH channel_enabled=true AND launching the session with
    # `--dangerously-load-development-channels`.
    channel_enabled: bool = False
    # Adapter identity / presence mailbox (falls back to peer_name when unset).
    channel_identity: str | None = None
    # One-line presence blurb the adapter heartbeats via announce().
    channel_summary: str | None = None

    # Logging
    log_level: str = "INFO"
    log_format: str = "console"

    # HTTP client (outbound to peers)
    peer_request_timeout: float = 30.0

    @property
    def db_full_path(self) -> Path:
        """Resolve and create parent dir of the sqlite store path."""
        path = Path(os.path.expanduser(self.db_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def workspace_roots_resolved(self) -> list[Path]:
        """Parse WORKSPACE_ROOTS into resolved Path objects."""
        if not self.workspace_roots.strip():
            return []
        out: list[Path] = []
        for part in self.workspace_roots.split(":"):
            part = part.strip()
            if not part:
                continue
            p = Path(os.path.expanduser(part)).resolve()
            out.append(p)
        return out

    def peer_by_name(self, name: str) -> PeerConfig | None:
        """Lookup peer by friendly name."""
        for p in self.peers:
            if p.name == name:
                return p
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
