"""Pub/sub channel tools — walkie-talkie style multi-agent coordination.

Channels live on a single peer's server. A controller broadcasts via publish();
any number of subscribers (other peers, or this peer's own worker) call
subscribe() to long-poll for new messages on the channel.

To broadcast across multiple peers, the caller publishes to each peer's server
in turn (this server does NOT replicate to other peers).
"""

from typing import Annotated, Any

from pydantic import Field

from ..errors import ValidationError, format_error_response
from ..logging_config import get_logger
from ..server import mcp, settings, store
from ..utils.validation import (
    validate_channel,
    validate_pubsub_payload,
    validate_timeout,
)

logger = get_logger(__name__)


@mcp.tool(
    description=(
        "[Any] Publish a message to a named channel on this peer. Returns the new "
        "message id; pass it (or 0) as `after_id` on the first subscribe() to start "
        "from a known point."
    )
)
async def publish(
    channel: Annotated[str, Field(description="Channel name (alphanumeric + . _ - :)")],
    payload: Annotated[dict[str, Any], Field(description="JSON object to broadcast")],
    sender: Annotated[
        str | None,
        Field(description="Sender peer name (for traceability)"),
    ] = None,
) -> dict[str, Any]:
    try:
        channel = validate_channel(channel)
        payload = validate_pubsub_payload(payload)
        sender_name = (sender or settings.peer_name).strip()[:64] or "unknown"

        new_id = await store.publish(channel, sender_name, payload)
        return {"success": True, "channel": channel, "id": new_id}
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Any] Long-poll for new messages on a channel published after `after_id`. "
        "Returns the next batch (oldest first) and the new high-water id. On timeout "
        "returns an empty list — call again with the same after_id to keep waiting."
    )
)
async def subscribe(
    channel: Annotated[str, Field(description="Channel name to listen on")],
    after_id: Annotated[
        int,
        Field(
            description=(
                "Return messages with id > this. Use 0 to start from the "
                "beginning of the retained window."
            ),
        ),
    ] = 0,
    timeout: Annotated[
        float | None,
        Field(description="Max seconds to block (capped by poll_max_wait_s)"),
    ] = None,
) -> dict[str, Any]:
    try:
        channel = validate_channel(channel)
        wait_s = validate_timeout(timeout, default=settings.poll_max_wait_s, cap=300.0)
        if not isinstance(after_id, int) or after_id < 0:
            raise ValidationError("after_id must be a non-negative integer", field="after_id")

        msgs = await store.wait_for_pubsub(channel, after_id, wait_s)
        new_after = max((m["id"] for m in msgs), default=after_id)
        return {
            "success": True,
            "channel": channel,
            "messages": msgs,
            "count": len(msgs),
            "after_id": new_after,
        }
    except ValidationError as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)
