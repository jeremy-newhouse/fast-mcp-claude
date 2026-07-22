"""Input validation helpers.

These are deliberately strict — the server's network surface includes file
write and pub/sub broadcast, so every tool that takes untrusted input must
funnel through these validators.
"""

import base64
import binascii
import json
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
# Structured side-channel fields (metadata/payload/result bags attached to a
# message rather than being the message itself) — same precedent as pubsub.
MAX_METADATA_BYTES = 256_000  # 256 KB
# tool_input can legitimately carry prompt-scale content (e.g. a large file
# write relayed through the permission hook), so it gets the prompt cap.
MAX_TOOL_INPUT_BYTES = 1_000_000  # 1 MB
MAX_TOOL_NAME_BYTES = 256
MAX_ATTACHMENT_NAME_BYTES = 256
MAX_ATTACHMENT_MIME_BYTES = 128


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

    # Check containment BEFORE touching the filesystem for existence, so a
    # rejected out-of-sandbox path never leaks (via error type or message)
    # whether it actually exists.
    for root in workspace_roots:
        try:
            resolved.relative_to(root)
            break
        except ValueError:
            continue
    else:
        raise PermissionDeniedError(
            f"{field} {resolved} is outside WORKSPACE_ROOTS",
            details={"allowed_roots": [str(r) for r in workspace_roots]},
        )

    if must_exist and not resolved.exists():
        raise ValidationError(f"{field} does not exist: {resolved}", field=field)

    return resolved


def validate_json_object_size(value: dict, *, max_bytes: int, field: str) -> dict:
    """Reject a structured (dict) field once its JSON encoding exceeds max_bytes.

    Shared by every tool that json.dumps's a caller-supplied dict straight into
    SQLite (metadata/payload/result/tool_input bags) — without this, such a
    field bypasses every other documented body-size cap.
    """
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be a JSON object", field=field)
    encoded = json.dumps(value).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValidationError(
            f"{field} exceeds {max_bytes} bytes",
            field=field,
        )
    return value


def validate_pubsub_payload(payload: dict, *, field: str = "payload") -> dict:
    return validate_json_object_size(payload, max_bytes=MAX_PUBSUB_PAYLOAD_BYTES, field=field)


def validate_metadata(value: dict | None, *, field: str = "metadata") -> dict | None:
    """Validate an optional structured metadata/payload/result field."""
    if value is None:
        return None
    return validate_json_object_size(value, max_bytes=MAX_METADATA_BYTES, field=field)


def validate_attachment(value: dict | None, *, field: str = "attachment") -> dict | None:
    """Validate an optional Teams-send file attachment: {name, mime, content_b64}.

    content_b64 is base64-DECODED here (not just length-checked) so a caller can't
    smuggle an oversized payload past a naive length check with non-standard padding —
    the decoded byte count is what's actually capped, against the same MAX_FILE_BYTES
    the file-bridge already uses (ECA-117).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be a JSON object", field=field)

    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError(f"{field}.name must be a non-empty string", field=field)
    if len(name.encode("utf-8")) > MAX_ATTACHMENT_NAME_BYTES:
        raise ValidationError(
            f"{field}.name exceeds {MAX_ATTACHMENT_NAME_BYTES} bytes", field=field
        )

    mime = value.get("mime")
    if not isinstance(mime, str) or not mime.strip():
        raise ValidationError(f"{field}.mime must be a non-empty string", field=field)
    if len(mime.encode("utf-8")) > MAX_ATTACHMENT_MIME_BYTES:
        raise ValidationError(
            f"{field}.mime exceeds {MAX_ATTACHMENT_MIME_BYTES} bytes", field=field
        )

    content_b64 = value.get("content_b64")
    if not isinstance(content_b64, str) or not content_b64:
        raise ValidationError(f"{field}.content_b64 must be a non-empty string", field=field)
    try:
        decoded = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValidationError(f"{field}.content_b64 is not valid base64", field=field) from e
    if not decoded:
        raise ValidationError(
            f"{field}.content_b64 must not decode to empty content", field=field
        )
    if len(decoded) > MAX_FILE_BYTES:
        raise ValidationError(f"{field} content exceeds {MAX_FILE_BYTES} bytes", field=field)

    return {"name": name.strip(), "mime": mime.strip(), "content_b64": content_b64}


def validate_tool_name(value: str, *, field: str = "tool_name") -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} is required", field=field)
    if len(value.encode("utf-8")) > MAX_TOOL_NAME_BYTES:
        raise ValidationError(f"{field} exceeds {MAX_TOOL_NAME_BYTES} bytes", field=field)
    return value


def validate_tool_input(value: dict, *, field: str = "tool_input") -> dict:
    return validate_json_object_size(value, max_bytes=MAX_TOOL_INPUT_BYTES, field=field)
