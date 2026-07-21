"""Channel → Teams outbox tools (ADR-0013).

A peer LIVE session (the fast-mcp-claude-channel sidecar) asks the hub to post a message to
Teams. The flow mirrors the approval bridge but on its own queue, fully isolated from the
approval and worker-message queues:

  1. CHANNEL (calls LOCAL server): request_teams_send(text, target?, metadata?) -> request_id,
     then await_teams_send(request_id) to get the delivery result.
  2. HUB / CONTROLLER (the eCA brain, calls the peer's server): wait_for_pending_teams_send()
     to drain pending requests, posts to Teams, then complete_teams_send(request_id, ok, detail)
     to unblock the channel's await.

The hub — not this server — decides WHETHER to post (it honors only `metadata.triggering_admin`
and resolves the target to a bot-known Teams conversation). This server is just the durable
relay queue. See the brain's TeamsOutboxWatcher.
"""

from typing import Annotated, Any

from pydantic import Field

from ..errors import NotFoundError, ValidationError, format_error_response
from ..logging_config import get_logger
from ..server import mcp, settings, store
from ..utils.validation import (
    validate_message_id,
    validate_metadata,
    validate_response,
    validate_session_id,
    validate_timeout,
)

logger = get_logger(__name__)

_MAX_TARGET = 256


@mcp.tool(
    description=(
        "[Channel] Ask the eCA hub to post a message to Teams. `target` is the destination "
        "chat name (omit to use the conversation that triggered this session). Returns "
        "request_id; call await_teams_send(request_id) to get the delivery result. The hub "
        "decides whether to post (admin-triggered sessions only) and resolves the chat name."
    )
)
async def request_teams_send(
    text: Annotated[str, Field(description="Message text to post to Teams")],
    target: Annotated[
        str | None,
        Field(description="Destination chat name (or known id); omit for the originating chat"),
    ] = None,
    metadata: Annotated[
        dict[str, Any] | None,
        Field(description="Context the hub trusts: triggering_admin, conversation_id, …"),
    ] = None,
    requester_session: Annotated[
        str | None,
        Field(description="This session's identity (for traceability)"),
    ] = None,
) -> dict[str, Any]:
    try:
        text = validate_response(text, field="text")
        if not text.strip():
            raise ValidationError("text must be non-empty", field="text")
        if target is not None:
            if not isinstance(target, str):
                raise ValidationError("target must be a string", field="target")
            target = target.strip()[:_MAX_TARGET] or None
        metadata = validate_metadata(metadata)
        requester = validate_session_id(requester_session, field="requester_session") or "default"

        request_id = await store.create_teams_send(
            requester=requester, text=text, target=target, metadata=metadata
        )
        logger.info(
            "Teams send requested",
            extra={"request_id": request_id, "requester": requester, "target": target},
        )
        return {"success": True, "request_id": request_id}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Hub] Long-poll until at least one pending Teams-send request exists, then return "
        "them. The hub posts each to Teams and calls complete_teams_send(). Empty list on "
        "timeout — call again to keep waiting."
    )
)
async def wait_for_pending_teams_send(
    timeout: Annotated[
        float | None,
        Field(description="Max seconds to block (capped at 300s)"),
    ] = None,
) -> dict[str, Any]:
    try:
        wait_s = validate_timeout(timeout, default=settings.poll_max_wait_s, cap=300.0)
        requests = await store.wait_for_pending_teams_sends(wait_s)
        return {"success": True, "requests": requests}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Hub] Complete a Teams-send request after posting (or failing). `ok` is whether the "
        "post landed; `detail` is the result/error shown to the requesting session. Unblocks "
        "the channel's await_teams_send()."
    )
)
async def complete_teams_send(
    request_id: Annotated[str, Field(description="ID from request_teams_send")],
    ok: Annotated[bool, Field(description="Whether the Teams post succeeded")],
    detail: Annotated[
        str | None, Field(description="Human-readable result/error")
    ] = None,
) -> dict[str, Any]:
    try:
        request_id = validate_message_id(request_id, field="request_id")
        if not isinstance(ok, bool):
            raise ValidationError("ok must be a boolean", field="ok")
        if detail is not None and not isinstance(detail, str):
            raise ValidationError("detail must be a string", field="detail")
        if detail and len(detail) > 4000:
            raise ValidationError("detail too long", field="detail")

        completed = await store.complete_teams_send(request_id, ok, detail)
        if not completed:
            return {
                "success": False,
                "error": {
                    "message": "Request not found or already completed",
                    "code": "NOT_COMPLETABLE",
                },
            }
        return {"success": True, "request_id": request_id}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Channel] Long-poll for the hub's result on a Teams-send request. Returns the record "
        "with ok/detail once the hub completes it, or {ready:false} on timeout — call again."
    )
)
async def await_teams_send(
    request_id: Annotated[str, Field(description="ID from request_teams_send")],
    timeout: Annotated[
        float | None,
        Field(description="Max seconds to block (capped at 300s)"),
    ] = None,
) -> dict[str, Any]:
    try:
        request_id = validate_message_id(request_id, field="request_id")
        wait_s = validate_timeout(timeout, default=settings.poll_max_wait_s, cap=300.0)

        record = await store.wait_for_teams_send_result(request_id, wait_s)
        if record is None:
            current = await store.get_teams_send(request_id)
            if current is None:
                raise NotFoundError(f"Unknown request_id: {request_id}")
            return {"success": True, "ready": False, "request_id": request_id}
        return {"success": True, "ready": True, "request": record}
    except (ValidationError, NotFoundError) as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)
