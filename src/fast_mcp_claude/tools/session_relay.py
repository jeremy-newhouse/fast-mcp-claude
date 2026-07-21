"""Session-to-session relay tools.

A peer LIVE session (the fast-mcp-claude-channel sidecar) asks the eCA hub — the only node
that spans every peer — to LIST the operator's other sessions or SEND a message to one. The
flow mirrors the channel->Teams outbox (ADR-0013) but on its OWN queue, fully isolated from
the approval, teams, and worker-message paths:

  1. SESSION (calls LOCAL server): request_session_op(op, payload?) -> request_id, then
     await_session_op(request_id) to get the hub's result.
  2. HUB / CONTROLLER (the eCA brain, calls the peer's server): wait_for_pending_session_ops()
     to drain pending ops, performs the cross-peer routing, then
     complete_session_op(request_id, ok, result) to unblock the session's await.

Why brain-mediated and not peer-to-peer: each peer's server binds 127.0.0.1 and is reachable
ONLY by the brain (over a forward SSH tunnel); `who()` is server-local, so a session sees only
its OWN peer's sessions. The brain holds the single global directory and the only path to every
peer, so list/send across machines must route through it. See the brain's SessionRelayWatcher
and ADR-0015.

`op` is 'list' or 'send'; `payload` (for send: {target, text, wait_for_reply, ...}) and `result`
are opaque JSON to this server — the hub interprets them. This server is just the durable queue.
"""

from typing import Annotated, Any

from pydantic import Field

from ..errors import NotFoundError, ValidationError, format_error_response
from ..logging_config import get_logger
from ..server import mcp, settings, store
from ..utils.validation import (
    validate_message_id,
    validate_metadata,
    validate_session_id,
    validate_timeout,
)

logger = get_logger(__name__)

_VALID_OPS = {"list", "send", "check"}


@mcp.tool(
    description=(
        "[Session] Ask the eCA hub to relay between your sessions. `op` is 'list' (enumerate "
        "your other live sessions across all machines) or 'send' (deliver a message to another "
        "session). `payload` carries op args (send: {target, text, wait_for_reply}). Returns "
        "request_id; call await_session_op(request_id) for the hub's result. The hub does the "
        "cross-peer routing — this server only queues the request."
    )
)
async def request_session_op(
    op: Annotated[str, Field(description="Operation: 'list' or 'send'")],
    payload: Annotated[
        dict[str, Any] | None,
        Field(description="Op arguments (send: {target, text, wait_for_reply, wait_seconds})"),
    ] = None,
    requester_session: Annotated[
        str | None,
        Field(description="This session's identity (the sender; for attribution + self-exclude)"),
    ] = None,
) -> dict[str, Any]:
    try:
        if not isinstance(op, str) or op not in _VALID_OPS:
            raise ValidationError(f"op must be one of {sorted(_VALID_OPS)}", field="op")
        payload = validate_metadata(payload, field="payload")
        requester = validate_session_id(requester_session, field="requester_session") or "default"

        request_id = await store.create_session_op(requester=requester, op=op, payload=payload)
        logger.info(
            "session-relay op requested",
            extra={"request_id": request_id, "requester": requester, "op": op},
        )
        return {"success": True, "request_id": request_id}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Hub] Long-poll until at least one pending session-relay op exists, then return them. "
        "The hub performs each op and calls complete_session_op(). Empty list on timeout — call "
        "again to keep waiting."
    )
)
async def wait_for_pending_session_ops(
    timeout: Annotated[
        float | None,
        Field(description="Max seconds to block (capped at 300s)"),
    ] = None,
) -> dict[str, Any]:
    try:
        wait_s = validate_timeout(timeout, default=settings.poll_max_wait_s, cap=300.0)
        requests = await store.wait_for_pending_session_ops(wait_s)
        return {"success": True, "requests": requests}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Hub] Complete a session-relay op after performing it. `ok` is whether it succeeded; "
        "`result` is the JSON payload returned to the requesting session (list: {sessions, ...}; "
        "send: {ready, reply|delivered, ...}). Unblocks the session's await_session_op()."
    )
)
async def complete_session_op(
    request_id: Annotated[str, Field(description="ID from request_session_op")],
    ok: Annotated[bool, Field(description="Whether the op succeeded")],
    result: Annotated[
        dict[str, Any] | None, Field(description="JSON result returned to the requester")
    ] = None,
) -> dict[str, Any]:
    try:
        request_id = validate_message_id(request_id, field="request_id")
        if not isinstance(ok, bool):
            raise ValidationError("ok must be a boolean", field="ok")
        result = validate_metadata(result, field="result")

        completed = await store.complete_session_op(request_id, ok, result)
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
        "[Session] Long-poll for the hub's result on a session-relay op. Returns the record with "
        "ok/result once the hub completes it, or {ready:false} on timeout — call again."
    )
)
async def await_session_op(
    request_id: Annotated[str, Field(description="ID from request_session_op")],
    timeout: Annotated[
        float | None,
        Field(description="Max seconds to block (capped at 300s)"),
    ] = None,
) -> dict[str, Any]:
    try:
        request_id = validate_message_id(request_id, field="request_id")
        wait_s = validate_timeout(timeout, default=settings.poll_max_wait_s, cap=300.0)

        record = await store.wait_for_session_op_result(request_id, wait_s)
        if record is None:
            current = await store.get_session_op(request_id)
            if current is None:
                raise NotFoundError(f"Unknown request_id: {request_id}")
            return {"success": True, "ready": False, "request_id": request_id}
        return {"success": True, "ready": True, "request": record}
    except (ValidationError, NotFoundError) as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)
