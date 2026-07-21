"""File-bridge tools — read/write the LOCAL workspace from a remote controller.

All paths are validated against WORKSPACE_ROOTS via validate_workspace_path()
which blocks traversal outside allowed roots (including via symlinks).
"""

import os
from typing import Annotated, Any

from pydantic import Field

from ..errors import PermissionDeniedError, ValidationError, format_error_response
from ..logging_config import get_logger
from ..server import mcp, settings
from ..utils.secure_fs import is_regular_file, secure_open_read, secure_open_write, secure_scandir
from ..utils.validation import (
    MAX_FILE_BYTES,
    MAX_FILE_LIST_ENTRIES,
    validate_workspace_path,
)

logger = get_logger(__name__)


@mcp.tool(
    description=(
        "[Controller] List entries in a directory on this peer. The path must be "
        "absolute and lie under one of the server's WORKSPACE_ROOTS."
    )
)
async def list_files(
    path: Annotated[str, Field(description="Absolute directory path within WORKSPACE_ROOTS")],
    include_hidden: Annotated[bool, Field(description="Include dotfiles")] = False,
) -> dict[str, Any]:
    try:
        roots = settings.workspace_roots_resolved
        resolved = validate_workspace_path(path, workspace_roots=roots, must_exist=True)
        if not resolved.is_dir():
            raise ValidationError(f"path is not a directory: {resolved}", field="path")

        entries: list[dict[str, Any]] = []
        try:
            with secure_scandir(resolved, roots) as it:
                for de in it:
                    if not include_hidden and de.name.startswith("."):
                        continue
                    try:
                        stat = de.stat(follow_symlinks=False)
                        entries.append(
                            {
                                "name": de.name,
                                "path": str(resolved / de.name),
                                "type": "dir"
                                if de.is_dir(follow_symlinks=False)
                                else ("symlink" if de.is_symlink() else "file"),
                                "size": stat.st_size if de.is_file(follow_symlinks=False) else None,
                                "modified": stat.st_mtime,
                            }
                        )
                    except OSError:
                        continue
                    if len(entries) >= MAX_FILE_LIST_ENTRIES:
                        break
        except OSError as e:
            # A path component was swapped (e.g. for a symlink pointing outside
            # WORKSPACE_ROOTS) between validation and this real scan -- fail closed.
            raise PermissionDeniedError(
                f"path changed after validation, refusing to list: {resolved}"
            ) from e

        return {"success": True, "path": str(resolved), "entries": entries, "count": len(entries)}
    except (ValidationError, PermissionDeniedError) as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Controller] Read a text file from this peer's workspace. Refuses files larger than ~10MB."
    )
)
async def read_file(
    path: Annotated[str, Field(description="Absolute file path within WORKSPACE_ROOTS")],
) -> dict[str, Any]:
    try:
        roots = settings.workspace_roots_resolved
        resolved = validate_workspace_path(path, workspace_roots=roots, must_exist=True)
        if not resolved.is_file():
            raise ValidationError(f"path is not a regular file: {resolved}", field="path")

        try:
            with secure_open_read(resolved, roots) as fd:
                if not is_regular_file(fd):
                    raise ValidationError(f"path is not a regular file: {resolved}", field="path")
                size = os.fstat(fd).st_size
                if size > MAX_FILE_BYTES:
                    raise ValidationError(
                        f"file too large ({size} > {MAX_FILE_BYTES})",
                        field="path",
                    )
                chunks: list[bytes] = []
                remaining = size
                while remaining > 0:
                    chunk = os.read(fd, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
        except OSError as e:
            # A path component was swapped (e.g. for a symlink pointing outside
            # WORKSPACE_ROOTS) between validation and this real read -- fail closed.
            raise PermissionDeniedError(
                f"path changed after validation, refusing to read: {resolved}"
            ) from e

        try:
            content = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = raw.decode("utf-8", errors="replace")
            encoding = "utf-8-replace"

        return {
            "success": True,
            "path": str(resolved),
            "size": size,
            "encoding": encoding,
            "content": content,
        }
    except (ValidationError, PermissionDeniedError) as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)


@mcp.tool(
    description=(
        "[Controller] Write/overwrite a text file in this peer's workspace. Creates "
        "parent directories as needed. Refuses content larger than ~10MB."
    )
)
async def write_file(
    path: Annotated[str, Field(description="Absolute file path within WORKSPACE_ROOTS")],
    content: Annotated[str, Field(description="Full file contents (UTF-8 text)")],
    overwrite: Annotated[
        bool,
        Field(description="If false, refuse to overwrite existing files"),
    ] = True,
) -> dict[str, Any]:
    try:
        if not isinstance(content, str):
            raise ValidationError("content must be a string", field="content")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValidationError(
                f"content too large ({len(encoded)} > {MAX_FILE_BYTES})",
                field="content",
            )

        roots = settings.workspace_roots_resolved
        resolved = validate_workspace_path(path, workspace_roots=roots, must_exist=False)

        try:
            with secure_open_write(resolved, roots, overwrite=overwrite) as fd:
                if not is_regular_file(fd):
                    raise ValidationError(
                        f"path exists and is not a regular file: {resolved}",
                        field="path",
                    )
                os.ftruncate(fd, 0)
                view = memoryview(encoded)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
        except FileExistsError as e:
            raise PermissionDeniedError(
                f"file exists and overwrite=false: {resolved}",
            ) from e
        except IsADirectoryError as e:
            raise ValidationError(
                f"path exists and is not a regular file: {resolved}",
                field="path",
            ) from e
        except OSError as e:
            # A path component was swapped (e.g. for a symlink pointing outside
            # WORKSPACE_ROOTS) between validation and this real write -- fail closed.
            raise PermissionDeniedError(
                f"path changed after validation, refusing to write: {resolved}"
            ) from e

        return {"success": True, "path": str(resolved), "bytes_written": len(encoded)}
    except (ValidationError, PermissionDeniedError) as e:
        return format_error_response(e)
    except Exception as e:
        return format_error_response(e)
