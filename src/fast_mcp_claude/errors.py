"""Standardized error types and response helpers.

All tool functions should return responses via build_response() / format_error_response()
so that callers get a uniform `{"success": bool, ...}` envelope.
"""

from typing import Any

from .logging_config import get_logger

logger = get_logger(__name__)


class ClaudeRemoteError(Exception):
    """Base exception for all server tool errors."""

    def __init__(
        self,
        message: str,
        code: str = "UNKNOWN_ERROR",
        status_code: int | None = None,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.field = field
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"message": self.message, "code": self.code}
        if self.status_code is not None:
            result["status_code"] = self.status_code
        if self.field is not None:
            result["field"] = self.field
        if self.details:
            result["details"] = self.details
        return result


class ValidationError(ClaudeRemoteError):
    """Input validation failed."""

    def __init__(
        self,
        message: str,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message=message,
            code="VALIDATION_ERROR",
            status_code=400,
            field=field,
            details=details,
        )


class NotFoundError(ClaudeRemoteError):
    """Requested resource (message, approval, peer, path) does not exist."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(
            message=message,
            code="NOT_FOUND",
            status_code=404,
            details=details,
        )


class PeerError(ClaudeRemoteError):
    """Error returned from a remote peer's MCP endpoint."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message=message,
            code="PEER_ERROR",
            status_code=status_code,
            details=details,
        )


class PermissionDeniedError(ClaudeRemoteError):
    """Operation denied (e.g. path outside workspace allowlist)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(
            message=message,
            code="PERMISSION_DENIED",
            status_code=403,
            details=details,
        )


class TimeoutError_(ClaudeRemoteError):
    """A long-poll wait exceeded its timeout (returned as success=true, item=null)."""

    def __init__(self, message: str = "Operation timed out"):
        super().__init__(message=message, code="TIMEOUT", status_code=408)


def format_error_response(error: Exception) -> dict[str, Any]:
    """Convert any exception to the standard `{success: false, error: {...}}` envelope."""
    if isinstance(error, ClaudeRemoteError):
        return {"success": False, "error": error.to_dict()}

    logger.exception("Unexpected error", extra={"error_type": type(error).__name__})
    return {
        "success": False,
        "error": {"message": "An unexpected error occurred", "code": "UNKNOWN_ERROR"},
    }


def build_response(success: bool, **data: Any) -> dict[str, Any]:
    """Build a standard tool response envelope.

    Usage:
        return build_response(True, message_id="abc", queued_at=...)
        return build_response(False, error="bad path", field="path")
    """
    if success:
        return {"success": True, **data}

    error = data.pop("error", None)
    field = data.pop("field", None)
    code = data.pop("code", "ERROR")
    err_dict: dict[str, Any] = {"message": str(error) if error else "Error", "code": code}
    if field:
        err_dict["field"] = field
    if data:
        err_dict["details"] = data
    return {"success": False, "error": err_dict}
