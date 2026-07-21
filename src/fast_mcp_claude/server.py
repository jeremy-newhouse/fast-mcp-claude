"""FastMCP server instance, lifespan, and tool-module registration.

Layout mirrors fast-mcp-jira: module-level globals (settings, store, peer_client,
mcp) constructed at import time, then tool modules imported for their side-effect
decorator registration.
"""

import time

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from .auth import ApiKeyVerifier
from .config import Settings, get_settings
from .logging_config import get_logger
from .services.store import Store

logger = get_logger(__name__)


def build_auth_provider(settings: Settings) -> ApiKeyVerifier | None:
    """Decide the FastMCP auth provider, failing closed on misconfiguration.

    Raises RuntimeError instead of starting an unauthenticated server when
    auth is enabled but no usable key is configured -- the prior fail-open
    behavior silently served every MCP tool to any caller with network access.
    """
    if settings.mcp_auth_enabled:
        if not settings.mcp_api_key:
            raise RuntimeError(
                "MCP_AUTH_ENABLED=true but MCP_API_KEY is not set (or empty). "
                "Refusing to start with authentication enabled and no usable key, "
                "since that would silently serve every MCP tool unauthenticated. "
                "Set MCP_API_KEY, or explicitly set MCP_AUTH_ENABLED=false for "
                "local-only use."
            )
        return ApiKeyVerifier(api_key=settings.mcp_api_key)
    logger.warning("MCP_AUTH_ENABLED=false - endpoint is UNAUTHENTICATED")
    return None


settings = get_settings()

# Persistent store: inbox/outbox, approvals, pub/sub
store = Store(settings)

# Authentication
auth_provider = build_auth_provider(settings)


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
