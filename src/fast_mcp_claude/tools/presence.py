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
from ..logging_config import SENSITIVE_LOG_FIELDS_EXACT, SENSITIVE_LOG_SUFFIXES, get_logger
from ..server import mcp, settings, store
from ..utils.validation import validate_identity, validate_metadata

logger = get_logger(__name__)


def _redact_peer_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip credential-shaped keys (announce_token, etc.) before who() exposes a
    peer's metadata over the wire, recursing into nested dicts since announce()'s
    metadata is arbitrary structured context. store.list_presence() itself is left
    untouched — it's an internal Store API (tests rely on it to verify the ECA-71
    owner-token guard end-to-end) and has no other caller besides this tool."""
    if not isinstance(metadata, dict):
        return metadata
    return {
        k: _redact_peer_metadata(v) if isinstance(v, dict) else v
        for k, v in metadata.items()
        if k.lower() not in SENSITIVE_LOG_FIELDS_EXACT
        and not k.lower().endswith(SENSITIVE_LOG_SUFFIXES)
    }


@mcp.tool(
    description=(
        "[Any] Announce this peer's presence and a short summary of what it is "
        "doing, so other sessions can discover it via who(). The channel adapter "
        "calls this on a heartbeat; humans rarely call it directly. If metadata "
        "carries an `announce_token`, presence is owner-guarded: a second live "
        "process reusing the same identity with a different token is refused "
        "(IDENTITY_LIVE_ELSEWHERE) while the first is still heartbeating."
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
        metadata = validate_metadata(metadata)

        result = await store.announce(identity, summary=summary, metadata=metadata)
        # Owner-token identity guard (ECA-71 / ADR-0029): the store refuses a second live
        # announcer for an identity already held by a different process. Propagate the refusal
        # so the sidecar/launcher can disarm its claim loop instead of clobbering presence.
        if not result.get("success"):
            return {"success": False, "identity": identity, "error": result.get("error")}
        return {"success": True, "identity": identity}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Any] Best-effort presence forget on a clean session exit (ECA-82). Requires the "
        "caller's own announce_token — the row is only deleted if it still carries that "
        "token, so this can never clobber a different (successor) process's presence. "
        "Shrinks the owner-token identity guard's claim-latency gap on a graceful relaunch "
        "(pm2 restart / operator relaunch) down to ~0; a hard crash skips this and still "
        "waits out the normal freshness window."
    )
)
async def forget(
    identity: Annotated[str, Field(description="The identity to forget")],
    announce_token: Annotated[
        str, Field(description="This process's own announce_token, as sent to announce()")
    ],
) -> dict[str, Any]:
    try:
        identity = validate_identity(identity)
        if not isinstance(announce_token, str) or not announce_token:
            raise ValidationError("announce_token is required", field="announce_token")
        deleted = await store.forget_presence(identity, expected_token=announce_token)
        return {"success": True, "identity": identity, "deleted": deleted}
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
        if stale_seconds is not None:
            try:
                stale_seconds = float(stale_seconds)
            except (TypeError, ValueError) as e:
                raise ValidationError(
                    "stale_seconds must be a number", field="stale_seconds"
                ) from e
        if stale_seconds is None:
            stale_seconds = float(settings.poll_heartbeat_s * 3)
        elif stale_seconds <= 0:
            stale_seconds = None
        peers = await store.list_presence(stale_after=stale_seconds)
        sanitized_peers = [
            {**p, "metadata": _redact_peer_metadata(p.get("metadata"))} for p in peers
        ]
        return {
            "success": True,
            "self": settings.peer_name,
            "peers": sanitized_peers,
            "count": len(sanitized_peers),
        }
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)
