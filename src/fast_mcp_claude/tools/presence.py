"""Presence / roster tools — N-way peer discovery.

These let many sessions (different developers, different machines) find each other
and address one another by identity rather than the controller/worker pairing.

Topology-agnostic:
  - HUB deployment: everyone's channel adapter announces to the one shared server,
    so `who` returns the whole team roster.
  - MESH deployment: each peer announces to its own server; `who` reflects whoever
    you query (presence is naturally a hub-friendly feature).

A live channel adapter calls announce() on a heartbeat; `who` filters by freshness
via stale_seconds so dead peers drop off without any explicit unregister.
"""

from typing import Annotated, Any

from pydantic import Field

from ..errors import ValidationError, format_error_response
from ..logging_config import get_logger
from ..server import mcp, settings, store
from ..utils.validation import validate_identity

logger = get_logger(__name__)


@mcp.tool(
    description=(
        "[Any] Announce this peer's presence and a short summary of what it is "
        "doing, so other sessions can discover it via who(). The channel adapter "
        "calls this on a heartbeat; humans rarely call it directly."
    )
)
async def announce(
    identity: Annotated[
        str,
        Field(description="Stable peer/developer identity (also a recipient_session mailbox)"),
    ],
    summary: Annotated[
        str | None,
        Field(description="One-line description of what this peer is currently working on"),
    ] = None,
    metadata: Annotated[
        dict[str, Any] | None,
        Field(description="Optional structured context (cwd, branch, model, etc.)"),
    ] = None,
) -> dict[str, Any]:
    try:
        identity = validate_identity(identity)
        if summary is not None:
            if not isinstance(summary, str):
                raise ValidationError("summary must be a string", field="summary")
            summary = summary.strip()[:280] or None
        if metadata is not None and not isinstance(metadata, dict):
            raise ValidationError("metadata must be an object", field="metadata")

        await store.announce(identity, summary=summary, metadata=metadata)
        return {"success": True, "identity": identity}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Any] List peers currently present on this server (freshest first). Use "
        "the returned `identity` values as recipient_session in send_prompt to "
        "message a specific peer. stale_seconds drops peers whose last heartbeat "
        "is older than that (default: ~3 heartbeats)."
    )
)
async def who(
    stale_seconds: Annotated[
        float | None,
        Field(description="Only return peers seen within this many seconds; null = all"),
    ] = None,
) -> dict[str, Any]:
    try:
        if stale_seconds is None:
            stale_seconds = float(settings.poll_heartbeat_s * 3)
        elif stale_seconds <= 0:
            stale_seconds = None
        peers = await store.list_presence(stale_after=stale_seconds)
        return {
            "success": True,
            "self": settings.peer_name,
            "peers": peers,
            "count": len(peers),
        }
    except Exception as e:
        return format_error_response(e)
