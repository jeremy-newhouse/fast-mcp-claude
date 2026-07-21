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
    # Non-admin permission routing (channel mode): seconds the sidecar waits on await_decision
    # for the brain's verdict before DEFAULT-DENYing a gated tool. Keep this ABOVE the brain's
    # approval_prompt_ttl_seconds (it auto-denies first) so a real deny lands rather than a
    # timeout. Admin turns auto-allow without any round-trip, so this only bounds non-admin.
    channel_decision_timeout_s: float = 300.0
    # Max seconds the sidecar waits for the agent's reply() to a pushed message before claiming
    # the next (the live session runs one turn at a time; a late reply still relays). High so a
    # long task isn't cut short.
    channel_reply_timeout_s: float = 1800.0
    # Comma-separated tools the channel permission relay lets through WITHOUT a Teams round-trip
    # even on a non-admin turn. Read-only by default so only consequential calls (Bash/Edit/
    # Write/...) prompt the operator in Teams. (Our own reply tool is always auto-allowed.)
    channel_auto_pass_tools: str = "Read,Glob,Grep"

    # Layer C non-consumption recovery (ECA-71 / ADR-0029). The sidecar must never hold a claim
    # it cannot inject: on a pushed message the consumer never processes, it BOUNCEs the sender
    # (mesh reply "consumer not live") instead of the old silent 30-min drop — always on. The
    # FAST liveness signal below shortcuts the full reply_timeout by watching the hook status
    # file: a live consumer advances updated_at within a few seconds of a push (UserPromptSubmit
    # -> status="working"). Spike #2 RESOLVED YES (2026-07-09, live-verified on mini2/eca1 with
    # the new sidecar): a channel-injected push DOES fire UserPromptSubmit, advancing updated_at
    # at turn START (~26s before the Stop bump on a 12s task) — so this is now DEFAULT-ON. Without
    # a status file the fast path is inert (+ a startup warning): the sidecar cannot tell dead from
    # slow, so it takes the ambiguous NEVER-BOUNCE path (waits reply_timeout, then leaves the
    # message un-finalized — pre-ECA-71 behavior; there is NO timeout bounce). Only a status-file
    # consumer that does not advance in-window bounces fast; a turn that DOES advance in-window gets
    # the full reply_timeout. KNOWN LIMITATION (ECA-83): a push queued behind a long (> window)
    # in-flight turn shows no in-window advance and can be false-bounced — hardening tracked there.
    # Override per-host with CHANNEL_LIVENESS_CHECK=0.
    channel_liveness_check_enabled: bool = True
    # Seconds after a push to wait for the status file's updated_at to advance before treating
    # the message as unconsumed (fast path only).
    channel_liveness_window_s: float = 90.0
    # After this many CONSECUTIVE non-consumptions the sidecar disarms its claim loop and
    # re-announces channel=false/status=degraded so the brain stops pushing and reroutes to
    # notify+pull. It re-arms when the status file shows the consumer live again.
    channel_degrade_after: int = 3

    # Live-session sidecar (fast-mcp-claude-session) — STRICT opt-in, default off. The
    # launch wrapper starts ONE per interactive dev session. It is the SOLE announcer of
    # that session's presence (role="live-session"), heartbeating announce() with a status
    # summary it reads from a local status file the CC hooks write — routing hook status
    # through the single announcer avoids the announce() upsert clobber (announce overwrites
    # summary AND metadata wholesale; two announcers on one identity would erase each other).
    # It also watches the LOCAL inbox for queued messages addressed to this session and
    # surfaces each as a macOS notification + statusline badge WITHOUT claiming it (notify+
    # pull: the operator's /fleet-inbox pull claims + replies). It NEVER pushes into the
    # session (Claude Code 2.1.x dropped the dev-channel push path) and NEVER spawns anything.
    # Identity is "{peer_name}.{repo}" (a live session), distinct from "{peer_name}_launcher".
    session_enabled: bool = False
    # JSON status file the session hooks write and the sidecar reads (the wrapper sets it
    # explicitly; empty -> ~/.fast-mcp-claude/sessions/<identity>.json).
    session_status_file: str = ""
    # Fire a macOS notification (osascript) when a new inbox message arrives for this session.
    session_notify: bool = True
    # Inbox poll + presence heartbeat cadence (seconds). Heartbeat stays under who()'s
    # default stale window (poll_heartbeat_s*3) so a live session shows as fresh.
    session_poll_s: int = 10
    session_heartbeat_s: int = 15

    # Launcher sidecar (fast-mcp-claude-launcher) — STRICT opt-in, default off so a
    # configured-but-unintended sidecar stays inert and never claims a task it can't
    # run. Identity is f"{peer_name}_launcher". When armed it long-polls the LOCAL
    # server's inbox for headless `claude -p` tasks and spawns them in an allowlisted
    # cwd with a tools ceiling. See launcher.py.
    launcher_enabled: bool = False
    # Colon-separated allowlist of directory roots a task's cwd may resolve under
    # (same syntax as workspace_roots / $PATH). Default "" = nothing allowed, so
    # every task is rejected until an operator opts cwds in.
    launcher_cwd_allowlist: str = ""
    # Comma-separated tool-spec ceiling passed to claude --allowedTools. A task's
    # allowed_tools must be a subset; an omitted allowed_tools uses this whole set.
    # Default "" = no tools auto-approved.
    launcher_tools_ceiling: str = ""
    # Max tasks to spawn concurrently (concurrency slot acquired BEFORE each claim).
    launcher_max_concurrent: int = 2
    # Hard wall-clock cap per task in seconds; also the default when the envelope
    # omits timeout_s. An envelope timeout_s above this is clamped down.
    launcher_task_timeout_s: float = 900.0
    # Byte budget for the JSON-encoded reply. Far below the server's 4 MB
    # validate_response cap so a reply is never rejected (which would hang the
    # controller until the 7-day TTL). result + stderr_tail are head/tail truncated
    # to fit this on the ENCODED size.
    launcher_reply_max_bytes: int = 262144  # 256 KB
    # Passed to claude --setting-sources for each spawned task. KEEP "" FOREVER = load
    # NO settings so the worker runs bare. "project" would let an allowlisted repo's
    # .claude/settings.json hooks execute arbitrary commands on this machine, BYPASSING
    # the tools ceiling (hooks are not gated by tool restrictions). Phase 3 arms the
    # approval hook via the INDEPENDENT --settings flag (launcher_approval_hook_enabled)
    # WITHOUT touching this — so repo settings/hooks are never loaded.
    launcher_setting_sources: str = ""
    # Phase 3 approval bridge: arm a launcher-controlled PreToolUse hook on every spawned
    # worker via claude's --settings flag (an INLINE JSON object, independent of
    # --setting-sources which stays ""). The hook relays each gated tool call to the
    # LOCAL fast-mcp-claude server (request_approval/await_decision); the evolv-coder-agent daemon
    # decides it via Teams. STRICT opt-in (default off): when off, workers spawn ungated
    # exactly as in Phase 2. The hook command is built from launcher-resolved values only
    # (never the repo), so an allowlisted repo cannot inject hook commands.
    launcher_approval_hook_enabled: bool = False
    # Seconds the worker's hook waits for a controller decision before falling back to a
    # local "ask" (passed to the hook as CRM_DECISION_TIMEOUT).
    launcher_approval_decision_timeout_s: float = 300.0
    # Comma-separated tools the hook lets through WITHOUT a controller round-trip (passed
    # as CRM_AUTO_PASS_TOOLS). Read-only tools by default so only consequential calls
    # (Bash/Edit/Write/...) prompt the operator in Teams.
    launcher_approval_auto_pass_tools: str = "Read,Glob,Grep"
    # When the approval hook is enabled, prove at startup that a --settings PreToolUse
    # hook actually FIRES under --setting-sources "" (claude silently ignores a --settings
    # object that fails validation, which would disarm the gate). On failure the launcher
    # refuses to arm (fail-closed). Set false only if the self-test proves flaky.
    launcher_approval_hook_selftest: bool = True
    # Unix-domain socket the spawned worker's hook talks to for approvals. The launcher
    # (which holds the mesh bearer) listens here and relays request_approval/await_decision
    # to the local server on the hook's behalf, so the WORKER never receives any mesh
    # credential (closing the argv/file leak that would let it self-approve). The path is
    # NOT a secret. Empty -> defaults to ~/.fast-mcp-claude/launcher-approval.sock.
    launcher_approval_socket_path: str = ""
    # The claude CLI binary; resolved via shutil.which at startup (hard-fail/idle if
    # missing — a claim we can't run would be lost work).
    launcher_claude_bin: str = "claude"

    # Logging
    log_level: str = "INFO"
    log_format: str = "console"

    # HTTP client (outbound to peers)
    peer_request_timeout: float = 30.0

    @property
    def mcp_auth_effective(self) -> bool:
        """Whether requests will actually be authenticated at runtime.

        Single source of truth for server.py's fail-closed startup check and
        __main__.py's startup log, so the two can't drift into reporting
        different answers for the same config (e.g. an empty-string API key).
        """
        return bool(self.mcp_api_key) and self.mcp_auth_enabled

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

    @property
    def launcher_cwd_allowlist_resolved(self) -> list[Path]:
        """Parse LAUNCHER_CWD_ALLOWLIST into resolved Path objects (mirrors
        workspace_roots_resolved: colon-split, expanduser, resolve/follow-symlinks)."""
        if not self.launcher_cwd_allowlist.strip():
            return []
        out: list[Path] = []
        for part in self.launcher_cwd_allowlist.split(":"):
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
