"""Race-safe filesystem primitives for the file-bridge tools (FMC-11).

validate_workspace_path() only proves containment at the instant it runs and
returns a plain Path -- a string. Reusing that string for a later os.scandir/
open/mkdir call re-traverses the filesystem from the root, so a co-located
process can swap a path component for a symlink pointing outside
WORKSPACE_ROOTS in the gap between validation and use (TOCTOU). Every helper
here instead walks component-by-component from an already-open, root-anchored
directory file descriptor and opens each remaining segment with O_NOFOLLOW via
dir_fd, so a symlink swapped in at any level raises OSError instead of being
followed.
"""

import os
import stat as stat_module
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..errors import PermissionDeniedError

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _root_for(resolved: Path, workspace_roots: list[Path]) -> Path:
    for root in workspace_roots:
        try:
            resolved.relative_to(root)
            return root
        except ValueError:
            continue
    raise PermissionDeniedError(f"{resolved} is outside WORKSPACE_ROOTS")


@contextmanager
def _walk_to_parent_fd(
    resolved: Path, workspace_roots: list[Path], *, create_missing: bool
) -> Iterator[tuple[int, str]]:
    """Walk from the matching workspace root to `resolved`'s parent directory.

    Yields (parent_dir_fd, final_component_name). Every intermediate open uses
    O_NOFOLLOW via dir_fd, so a symlink swapped in for any ancestor directory
    raises OSError instead of being traversed.
    """
    root = _root_for(resolved, workspace_roots)
    parts = resolved.relative_to(root).parts
    if not parts:
        raise PermissionDeniedError(f"{resolved} is a workspace root, not a file")

    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=dir_fd)
            except FileNotFoundError:
                if not create_missing:
                    raise
                try:
                    os.mkdir(part, dir_fd=dir_fd)
                except FileExistsError as e:
                    # Something else created (or swapped in) `part` between our
                    # lookup and our mkdir -- never trust it, fail closed.
                    raise NotADirectoryError(
                        f"path component {part!r} changed during directory creation"
                    ) from e
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        yield dir_fd, parts[-1]
    finally:
        os.close(dir_fd)


@contextmanager
def secure_scandir(resolved: Path, workspace_roots: list[Path]) -> Iterator[Any]:
    """Open `resolved` itself as a no-follow-verified directory and scandir it."""
    root = _root_for(resolved, workspace_roots)
    parts = resolved.relative_to(root).parts
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        with os.scandir(dir_fd) as it:
            yield it
    finally:
        os.close(dir_fd)


@contextmanager
def secure_open_read(resolved: Path, workspace_roots: list[Path]) -> Iterator[int]:
    """Open `resolved` for reading; every path component is verified with O_NOFOLLOW."""
    with _walk_to_parent_fd(resolved, workspace_roots, create_missing=False) as (parent_fd, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            yield fd
        finally:
            os.close(fd)


@contextmanager
def secure_open_write(
    resolved: Path, workspace_roots: list[Path], *, overwrite: bool
) -> Iterator[int]:
    """Open `resolved` for writing, creating missing parent directories.

    Every path component (including newly-created parents) is verified with
    O_NOFOLLOW. overwrite=False makes the existence check atomic via O_EXCL
    instead of a separate, racy Path.exists() call.
    """
    with _walk_to_parent_fd(resolved, workspace_roots, create_missing=True) as (parent_fd, name):
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
        if not overwrite:
            flags |= os.O_EXCL
        fd = os.open(name, flags, 0o644, dir_fd=parent_fd)
        try:
            yield fd
        finally:
            os.close(fd)


def is_regular_file(fd: int) -> bool:
    return stat_module.S_ISREG(os.fstat(fd).st_mode)
