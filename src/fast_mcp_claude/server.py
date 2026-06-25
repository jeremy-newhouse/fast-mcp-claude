"""FastMCP server instance, lifespan, and tool-module registration.

Layout mirrors fast-mcp-jira: module-level globals (settings, store, peer_client,
mcp) constructed at import time, then tool modules imported for their side-effect
decorator registration.
"""

import time

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from .auth import ApiKeyVerifier
from .config import get_settings
from .logging_config import get_logger
from .services.store import Store

logger = get_logger(__name__)

settings = get_settings()

# Persistent store: inbox/outbox, approvals, pub/sub
store = Store(settings)

# Authentication
auth_provider: ApiKeyVerifier | None = None
if settings.mcp_api_key and settings.mcp_auth_enabled:
    auth_provider = ApiKeyVerifier(api_key=settings.mcp_api_key)
elif not settings.mcp_auth_enabled:
    logger.warning("MCP_AUTH_ENABLED=false - endpoint is UNAUTHENTICATED")
else:
    logger.warning(
        "MCP_API_KEY not set - endpoint is UNAUTHENTICATED. "
        "Set MCP_API_KEY for any non-localhost deployment."
    )


@lifespan
async def app_lifespan(server: FastMCP) -> dict:
    """Initialize services on startup; close them on shutdown."""
    logger.info("Starting fast-mcp-claude services")
    await store.initialize()
    logger.info("Store initialized", extra={"db_path": str(settings.db_full_path)})

    try:
        yield {"store": store, "settings": settings}
    finally:
        logger.info("Shutdown initiated")
        start = time.time()

        try:
            await store.close()
            logger.info(
                "Store closed",
                extra={"elapsed_ms": round((time.time() - start) * 1000, 2)},
            )
        except Exception as e:
            logger.error("Store close failed", extra={"error": str(e)})

        logger.info(
            "Shutdown complete",
            extra={"total_ms": round((time.time() - start) * 1000, 2)},
        )


mcp = FastMCP(
    name="fast-mcp-claude",
    instructions=(
        "Peer-to-peer remote control between Claude Code sessions.\n\n"
        "Each peer runs this server locally and registers OTHER peers' URLs in .mcp.json.\n"
        "To control peer B from peer A: A's Claude calls tools on B's MCP endpoint.\n\n"
        "Tool groups:\n"
        "  Messaging:  send_prompt, wait_for_completion, get_status, interrupt,\n"
        "              cancel, list_messages, wait_for_instruction, reply,\n"
        "              consume_interrupt\n"
        "  Permissions:request_approval (hook-only), pending_approvals,\n"
        "              wait_for_pending_approval, approve_tool, await_decision\n"
        "  TeamsOutbox:request_teams_send, await_teams_send (channel),\n"
        "              wait_for_pending_teams_send, complete_teams_send (hub)\n"
        "  SessionRelay:request_session_op, await_session_op (session),\n"
        "              wait_for_pending_session_ops, complete_session_op (hub)\n"
        "  Files:      list_files, read_file, write_file  (sandboxed to\n"
        "              WORKSPACE_ROOTS)\n"
        "  Pub/sub:    publish, subscribe\n"
        "  Presence:   announce, who  (N-way peer discovery; address a peer by\n"
        "              passing its identity as recipient_session)\n\n"
        "message_id is the correlation key for send_prompt <-> reply <-> wait_for_completion."
    ),
    auth=auth_provider,
    lifespan=app_lifespan,
)


# Import tool modules for side-effect registration via @mcp.tool decorators
from .tools import (  # noqa: E402, F401
    files,
    messaging,
    permissions,
    presence,
    pubsub,
    session_relay,
    teams_outbox,
)
