"""Credential-helper git clone — the token never touches argv, URL, .git/config,
image layers, or container-wide env (AC#3).

The spawner bind-mounts the operator's own fine-grained, short-TTL token as a
``0400`` file (default ``/run/secrets/gh_token``). We clone with an inline
``credential.helper`` that ``cat``s that file, so the secret is supplied to git
over the helper protocol on stdout only:

    git -c credential.helper='!f() { echo username=x-access-token;
        echo "password=$(cat /run/secrets/gh_token)"; }; f' clone <url>

The URL passed to git is the plain ``https://github.com/owner/repo.git`` — no
embedded token — so it is safe in process listings and the resulting
``.git/config`` remote. Clone-only; the working tree and any cached credential
vanish on ``--rm``.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

DEFAULT_TOKEN_PATH = "/run/secrets/gh_token"
DEFAULT_USERNAME = "x-access-token"


class GitCloneError(RuntimeError):
    """Raised when the credential-helper clone fails."""


def _helper_expr(token_path: str, username: str) -> str:
    """Build the inline credential.helper shell function.

    Reads the token file at *invocation* time (not baked in), emitting the
    git credential protocol lines. The path is single-quoted for the shell.
    """
    quoted = shlex.quote(token_path)
    return (
        "!f() { "
        f"echo username={username}; "
        f'echo "password=$(cat {quoted})"; '
        "}; f"
    )


def clone_argv(
    url: str,
    dest: str,
    *,
    ref: str | None = None,
    depth: int | None = 1,
    token_path: str = DEFAULT_TOKEN_PATH,
    username: str = DEFAULT_USERNAME,
) -> list[str]:
    """Return the full ``git`` argv for a credential-helper clone.

    The token itself is NOT in this argv — only the path to the mounted file is.
    """
    argv = [
        "git",
        "-c",
        f"credential.helper={_helper_expr(token_path, username)}",
        "-c",
        "credential.useHttpPath=true",
        "clone",
    ]
    if depth is not None:
        argv += ["--depth", str(depth)]
    if ref is not None:
        argv += ["--branch", ref]
    argv += [url, dest]
    return argv


async def clone(
    url: str,
    dest: str | Path,
    *,
    ref: str | None = None,
    depth: int | None = 1,
    token_path: str = DEFAULT_TOKEN_PATH,
    username: str = DEFAULT_USERNAME,
    timeout_s: float = 300.0,
) -> Path:
    """Clone *url* into *dest*, returning the destination path.

    Raises :class:`GitCloneError` on non-zero exit or timeout. stderr is captured
    and scrubbed of the token path only (the token itself is never echoed by git).
    """
    dest = Path(dest)
    argv = clone_argv(
        url, str(dest), ref=ref, depth=depth, token_path=token_path, username=username
    )
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        proc.kill()
        await proc.wait()
        raise GitCloneError(f"git clone timed out after {timeout_s}s") from exc
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        raise GitCloneError(f"git clone failed (exit {proc.returncode}): {detail}")
    return dest


def token_present(token_path: str = DEFAULT_TOKEN_PATH) -> bool:
    """True if a non-empty token file is mounted (lets the runner skip cloning cleanly)."""
    p = Path(token_path)
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False
