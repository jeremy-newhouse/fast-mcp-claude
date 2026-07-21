"""Permission-relay tools.

Flow:
  1. Worker peer's PreToolUse hook fires; the hook calls request_approval()
     and blocks on await_decision() (both on the worker's LOCAL server).
  2. The controlling peer's Claude calls pending_approvals() (or the long-poll
     variant wait_for_pending_approval()) on the worker's server to discover
     the request.
  3. The controller calls approve_tool(approval_id, decision, reason?) to
     decide; this unblocks await_decision() on the worker side, and the hook
     returns the decision to Claude Code.
"""

from typing import Annotated, Any

from pydantic import Field

from ..errors import NotFoundError, ValidationError, format_error_response
from ..logging_config import get_logger
from ..server import mcp, settings, store
from ..services.store import DECISION_ALLOW, DECISION_DENY
from ..utils.validation import (
    validate_approval_id,
    validate_session_id,
    validate_timeout,
    validate_tool_input,
    validate_tool_name,
)

logger = get_logger(__name__)


@mcp.tool(
    description=(
        "[Hook-internal] Create a pending permission request from a PreToolUse hook. "
        "Returns approval_id; the hook should then call await_decision(approval_id) "
        "to block until the controller responds."
    )
)
async def request_approval(
    session_id: Annotated[str, Field(description="Worker session id")],
    tool_name: Annotated[str, Field(description="Tool name being requested (e.g. Bash, Edit)")],
    tool_input: Annotated[dict[str, Any], Field(description="Tool input arguments")],
) -> dict[str, Any]:
    try:
        session_id = validate_session_id(session_id) or "default"
        tool_name = validate_tool_name(tool_name)
        tool_input = validate_tool_input(tool_input)

        approval_id = await store.create_approval(
            session_id=session_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        logger.info(
            "Approval requested",
            extra={"approval_id": approval_id, "tool_name": tool_name, "session_id": session_id},
        )
        return {"success": True, "approval_id": approval_id}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Hook-internal] Long-poll for a controller's decision on approval_id. Returns "
        "the approval record once decided, or {ready:false} on timeout (the hook can "
        "retry or default to deny)."
    )
)
async def await_decision(
    approval_id: Annotated[str, Field(description="ID returned by request_approval")],
    timeout: Annotated[
        float | None,
        Field(description="Max seconds to block (capped at 600s)"),
    ] = None,
) -> dict[str, Any]:
    try:
        approval_id = validate_approval_id(approval_id)
        wait_s = validate_timeout(timeout, default=settings.poll_max_wait_s, cap=600.0)

        approval = await store.wait_for_approval_decision(approval_id, wait_s)
        if approval is None:
            current = await store.get_approval(approval_id)
            if current is None:
                raise NotFoundError(f"Unknown approval_id: {approval_id}")
            return {"success": True, "ready": False, "approval_id": approval_id}
        return {"success": True, "ready": True, "approval": approval}
    except (ValidationError, NotFoundError) as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Controller] List pending permission requests on this peer. The controller "
        "calls this on the worker's server, evaluates each, then calls approve_tool()."
    )
)
async def pending_approvals(
    limit: Annotated[int, Field(description="Max rows (1-200)")] = 50,
) -> dict[str, Any]:
    try:
        try:
            limit = int(limit)
        except (TypeError, ValueError) as e:
            raise ValidationError("limit must be an integer", field="limit") from e
        limit = max(1, min(limit, 200))
        rows = await store.list_pending_approvals(limit=limit)
        return {"success": True, "approvals": rows, "count": len(rows)}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Controller] Long-poll until at least one pending approval exists, then "
        "return them. Use instead of pending_approvals() to avoid busy-polling."
    )
)
async def wait_for_pending_approval(
    timeout: Annotated[
        float | None,
        Field(description="Max seconds to block (capped at 300s)"),
    ] = None,
) -> dict[str, Any]:
    try:
        wait_s = validate_timeout(timeout, default=settings.poll_max_wait_s, cap=300.0)
        result = await store.wait_for_pending_approvals(wait_s)
        return {"success": True, "approvals": result}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Controller] Decide a pending permission request. decision must be 'allow' "
        "or 'deny'. The worker's await_decision() will unblock and the hook returns "
        "this decision to Claude Code."
    )
)
async def approve_tool(
    approval_id: Annotated[str, Field(description="ID from pending_approvals")],
    decision: Annotated[str, Field(description="'allow' or 'deny'")],
    reason: Annotated[
        str | None,
        Field(description="Optional human-readable rationale (shown to the worker model)"),
    ] = None,
) -> dict[str, Any]:
    try:
        approval_id = validate_approval_id(approval_id)
        if decision not in (DECISION_ALLOW, DECISION_DENY):
            raise ValidationError(
                "decision must be 'allow' or 'deny'",
                field="decision",
            )
        if reason is not None and not isinstance(reason, str):
            raise ValidationError("reason must be a string", field="reason")
        if reason and len(reason) > 4000:
            raise ValidationError("reason too long", field="reason")

        ok = await store.decide_approval(approval_id, decision, reason)
        if not ok:
            return {
                "success": False,
                "error": {
                    "message": "Approval not found or already decided",
                    "code": "NOT_DECIDABLE",
                },
            }
        return {"success": True, "approval_id": approval_id, "decision": decision}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)
