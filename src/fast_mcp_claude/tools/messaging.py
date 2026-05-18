"""Messaging tools: send/receive prompts between Claude Code peers.

Two roles use these tools:
  CONTROLLER (calls REMOTE peer's server):
    send_prompt, wait_for_completion, get_status, interrupt,
    cancel, list_messages
  WORKER (calls LOCAL server, run by the peer being controlled):
    wait_for_instruction, reply, consume_interrupt, get_status
"""

from typing import Annotated, Any

from pydantic import Field

from .. import __version__
from ..errors import NotFoundError, ValidationError, format_error_response
from ..logging_config import get_logger
from ..server import mcp, settings, store
from ..utils.validation import (
    validate_message_id,
    validate_prompt,
    validate_response,
    validate_session_id,
    validate_timeout,
)

logger = get_logger(__name__)


# ============================================================ CONTROLLER tools


@mcp.tool(
    description=(
        "[Controller] Send a prompt to this peer's inbox. The remote Claude session "
        "running the worker loop will pick it up via wait_for_instruction. Returns a "
        "message_id you can pass to wait_for_completion."
    )
)
async def send_prompt(
    prompt: Annotated[str, Field(description="The user-message text to inject into the remote session")],
    sender: Annotated[
        str | None,
        Field(description="Friendly name of the calling peer (for traceability)"),
    ] = None,
    recipient_session: Annotated[
        str | None,
        Field(description="If set, only a worker running with this session_id will pick it up; otherwise the next idle worker gets it"),
    ] = None,
    metadata: Annotated[
        dict[str, Any] | None,
        Field(description="Arbitrary JSON metadata attached to the message"),
    ] = None,
) -> dict[str, Any]:
    try:
        prompt = validate_prompt(prompt)
        recipient_session = validate_session_id(recipient_session, field="recipient_session")
        sender_name = (sender or "unknown").strip()[:64] or "unknown"

        message_id = await store.enqueue_message(
            sender=sender_name,
            prompt=prompt,
            recipient_session=recipient_session,
            metadata=metadata,
        )
        logger.info(
            "Message queued",
            extra={
                "message_id": message_id,
                "sender": sender_name,
                "recipient_session": recipient_session,
            },
        )
        return {"success": True, "message_id": message_id}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Controller] Long-poll for the worker's reply to a message_id. Returns the "
        "full message record (with response field set) when the worker calls reply(), "
        "or {success:true, ready:false} on timeout — call again to keep waiting."
    )
)
async def wait_for_completion(
    message_id: Annotated[str, Field(description="ID returned from send_prompt")],
    timeout: Annotated[
        float | None,
        Field(description="Max seconds to wait (capped by server's poll_max_wait_s)"),
    ] = None,
) -> dict[str, Any]:
    try:
        message_id = validate_message_id(message_id)
        wait_s = validate_timeout(timeout, default=settings.poll_max_wait_s, cap=300.0)

        msg = await store.wait_for_reply(message_id, wait_s)
        if msg is None:
            current = await store.get_message(message_id)
            if current is None:
                raise NotFoundError(f"Unknown message_id: {message_id}")
            return {
                "success": True,
                "ready": False,
                "status": current["status"],
                "message_id": message_id,
            }
        return {"success": True, "ready": True, "message": msg}
    except ValidationError as e:
        return format_error_response(e)
    except NotFoundError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Controller / Worker] Report this peer's identity, version, and live counts "
        "(queued messages, pending approvals)."
    )
)
async def get_status() -> dict[str, Any]:
    try:
        queued = await store.list_messages(status="queued", limit=1000)
        delivered = await store.list_messages(status="delivered", limit=1000)
        pending = await store.list_pending_approvals(limit=1000)
        return {
            "success": True,
            "peer_name": settings.peer_name,
            "version": __version__,
            "queued_count": len(queued),
            "in_progress_count": len(delivered),
            "pending_approvals_count": len(pending),
            "known_peers": [p.name for p in settings.peers],
            "workspace_roots": [str(p) for p in settings.workspace_roots_resolved],
        }
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Controller] Request the worker to interrupt its current turn. The worker's "
        "loop polls consume_interrupt() between turns; on the next check it will stop."
    )
)
async def interrupt(
    session_id: Annotated[
        str | None,
        Field(description="Worker session_id to interrupt; default 'default'"),
    ] = None,
) -> dict[str, Any]:
    try:
        session_id = validate_session_id(session_id) or "default"
        await store.request_interrupt(session_id)
        return {"success": True, "session_id": session_id}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Controller] Cancel a queued or in-flight message. wait_for_completion callers "
        "will wake with status=cancelled."
    )
)
async def cancel(
    message_id: Annotated[str, Field(description="ID returned from send_prompt")],
) -> dict[str, Any]:
    try:
        message_id = validate_message_id(message_id)
        ok = await store.cancel_message(message_id)
        if not ok:
            return {"success": False, "error": {
                "message": "Message not found or already finalized",
                "code": "NOT_CANCELLABLE",
            }}
        return {"success": True, "message_id": message_id}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Controller / Worker] List recent messages on this peer. Useful for "
        "observability / debugging the queue."
    )
)
async def list_messages(
    status: Annotated[
        str | None,
        Field(description="Filter: queued, delivered, replied, cancelled, expired"),
    ] = None,
    limit: Annotated[int, Field(description="Max rows (1-200)")] = 50,
) -> dict[str, Any]:
    try:
        if status and status not in {"queued", "delivered", "replied", "cancelled", "expired"}:
            raise ValidationError(f"unknown status: {status}", field="status")
        limit = max(1, min(int(limit), 200))
        rows = await store.list_messages(status=status, limit=limit)
        return {"success": True, "messages": rows, "count": len(rows)}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


# ============================================================ WORKER tools


@mcp.tool(
    description=(
        "[Worker] Long-poll this peer's inbox for the next prompt addressed to "
        "`recipient_session` (or any session if omitted). Returns {success:true, "
        "message: {...}} when a message is available, or {success:true, message:null} "
        "on timeout. The worker loop should call this in a tight loop."
    )
)
async def wait_for_instruction(
    recipient_session: Annotated[
        str | None,
        Field(description="Only pull messages addressed to this session_id (or unaddressed/broadcast). Default: receive any."),
    ] = None,
    timeout: Annotated[
        float | None,
        Field(description="Max seconds to block; capped by server's poll_max_wait_s"),
    ] = None,
) -> dict[str, Any]:
    try:
        recipient_session = validate_session_id(recipient_session, field="recipient_session")
        wait_s = validate_timeout(timeout, default=settings.poll_max_wait_s, cap=300.0)

        msg = await store.wait_for_next_for_worker(recipient_session, wait_s)
        return {"success": True, "message": msg}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Worker] Post the worker's response/result for a message_id. This unblocks "
        "any controller waiting on wait_for_completion for that message."
    )
)
async def reply(
    message_id: Annotated[str, Field(description="ID from the message you received")],
    response: Annotated[str, Field(description="Result text or JSON-encoded structured response")],
) -> dict[str, Any]:
    try:
        message_id = validate_message_id(message_id)
        response = validate_response(response)
        ok = await store.record_reply(message_id, response)
        if not ok:
            return {"success": False, "error": {
                "message": "Message not found or already finalized",
                "code": "NOT_REPLIABLE",
            }}
        return {"success": True, "message_id": message_id}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Worker] Check (and clear) whether the controller has requested an interrupt "
        "for this session. Returns {success:true, interrupted:bool}. Worker should "
        "call this between turns; if true, stop the current task."
    )
)
async def consume_interrupt(
    session_id: Annotated[
        str | None,
        Field(description="Worker session_id; default 'default'"),
    ] = None,
) -> dict[str, Any]:
    try:
        session_id = validate_session_id(session_id) or "default"
        had_one = await store.consume_interrupt(session_id)
        return {"success": True, "interrupted": had_one, "session_id": session_id}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)
