"""Entry point for the Fast MCP Claude server."""

from .config import get_settings
from .logging_config import get_logger, setup_logging


def main() -> None:
    """Run the Fast MCP Claude remote-control server."""
    settings = get_settings()

    setup_logging(
        level=settings.log_level,
        json_format=settings.log_format.lower() == "json",
    )

    logger = get_logger(__name__)

    from .server import mcp

    logger.info("Starting Fast MCP Claude server")
    logger.info(
        "Server configuration",
        extra={
            "peer_name": settings.peer_name,
            "host": settings.mcp_host,
            "port": settings.mcp_port,
            "auth_enabled": settings.mcp_api_key is not None and settings.mcp_auth_enabled,
            "known_peers": [p.name for p in settings.peers],
            "workspace_roots": [str(p) for p in settings.workspace_roots_resolved],
        },
    )

    mcp.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
