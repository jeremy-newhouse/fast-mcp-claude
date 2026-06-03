"""Input validation helpers.

These are deliberately strict — the server's network surface includes file
write and pub/sub broadcast, so every tool that takes untrusted input must
funnel through these validators.
"""

import re
from pathlib import Path

from ..errors import PermissionDeniedError, ValidationError

# Format constraints
SESSION_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")
CHANNEL_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")
PEER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MESSAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
APPROVAL_ID_RE = MESSAGE_ID_RE

# Body-size caps (prevent abuse)
MAX_PROMPT_BYTES = 1_000_000  # 1 MB
MAX_RESPONSE_BYTES = 4_000_000  # 4 MB
MAX_FILE_BYTES = 10_000_000  # 10 MB
MAX_FILE_LIST_ENTRIES = 1000
MAX_PUBSUB_PAYLOAD_BYTES = 256_000  # 256 KB


def validate_session_id(value: str | None, *, field: str = "session_id") -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string", field=field)
    if not SESSION_RE.match(value):
        raise ValidationError(
            f"{field} must match {SESSION_RE.pattern}",
            field=field,
        )
    return value


def validate_identity(value: str, *, field: str = "identity") -> str:
    """A peer/developer identity — same charset as a session id, but required.

    Doubles as a mailbox key: send_prompt(recipient_session=<identity>) routes to
    the worker whose channel adapter long-polls with that identity.
    """
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} is required", field=field)
    if not SESSION_RE.match(value):
        raise ValidationError(
            f"{field} must match {SESSION_RE.pattern}",
            field=field,
        )
    return value


def validate_channel(value: str, *, field: str = "channel") -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} is required", field=field)
    if not CHANNEL_RE.match(value):
        raise ValidationError(
            f"{field} must match {CHANNEL_RE.pattern}",
            field=field,
        )
    return value


def validate_peer_name(value: str, *, field: str = "peer") -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} is required", field=field)
    if not PEER_NAME_RE.match(value):
        raise ValidationError(
            f"{field} must match {PEER_NAME_RE.pattern}",
            field=field,
        )
    return value


def validate_message_id(value: str, *, field: str = "message_id") -> str:
    if not isinstance(value, str) or not MESSAGE_ID_RE.match(value):
        raise ValidationError(f"{field} must be a 32-char hex id", field=field)
    return value


def validate_approval_id(value: str, *, field: str = "approval_id") -> str:
    if not isinstance(value, str) or not APPROVAL_ID_RE.match(value):
        raise ValidationError(f"{field} must be a 32-char hex id", field=field)
    return value


def validate_prompt(value: str, *, field: str = "prompt") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string", field=field)
    if len(value.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValidationError(
            f"{field} exceeds {MAX_PROMPT_BYTES} bytes",
            field=field,
        )
    return value


def validate_response(value: str, *, field: str = "response") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string", field=field)
    if len(value.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValidationError(
            f"{field} exceeds {MAX_RESPONSE_BYTES} bytes",
            field=field,
        )
    return value


def validate_timeout(value: float | int | None, *, default: float, cap: float) -> float:
    if value is None:
        return float(default)
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise ValidationError("timeout must be a number", field="timeout") from e
    if v < 0:
        raise ValidationError("timeout must be >= 0", field="timeout")
    return min(v, cap)


def validate_workspace_path(
    raw: str,
    *,
    workspace_roots: list[Path],
    must_exist: bool = False,
    field: str = "path",
) -> Path:
    """Resolve `raw` to an absolute path and verify it sits under an allowed root.

    Blocks:
      - empty / non-string / null-byte paths
      - paths that resolve outside the workspace_roots allowlist
      - symlink escapes (uses Path.resolve(strict=False) and re-checks parent)
    """
    if not workspace_roots:
        raise PermissionDeniedError(
            "File bridge disabled (WORKSPACE_ROOTS is empty)",
        )
    if not isinstance(raw, str) or not raw:
        raise ValidationError(f"{field} is required", field=field)
    if "\x00" in raw:
        raise ValidationError(f"{field} contains null byte", field=field)
    if len(raw) > 4096:
        raise ValidationError(f"{field} too long", field=field)

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValidationError(
            f"{field} must be an absolute path",
            field=field,
        )

    # Resolve to canonical form (follows symlinks where they exist).
    resolved = candidate.resolve(strict=False)

    if must_exist and not resolved.exists():
        raise ValidationError(f"{field} does not exist: {resolved}", field=field)

    # Check it sits under at least one allowed root (also resolved).
    for root in workspace_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue

    raise PermissionDeniedError(
        f"{field} {resolved} is outside WORKSPACE_ROOTS",
        details={"allowed_roots": [str(r) for r in workspace_roots]},
    )


def validate_pubsub_payload(payload: dict, *, field: str = "payload") -> dict:
    if not isinstance(payload, dict):
        raise ValidationError(f"{field} must be a JSON object", field=field)
    import json

    encoded = json.dumps(payload).encode("utf-8")
    if len(encoded) > MAX_PUBSUB_PAYLOAD_BYTES:
        raise ValidationError(
            f"{field} exceeds {MAX_PUBSUB_PAYLOAD_BYTES} bytes",
            field=field,
        )
    return payload
