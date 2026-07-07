"""Container launch against the ECA-64 hardened image (ECA-65 AC#3).

The spawner is the *only* launcher: it owns the run flags, the secret bind-mounts, the egress
network wiring, and the job-dir relay. This module mirrors the hardened ``docker run``
invocation proven in ``sandbox-runner/smoke/smoke.sh`` (the smoke plays the spawner's role
before the spawner existed, so it IS the launch contract):

  * ``--network <egress-internal>`` + ``HTTPS_PROXY=http://<proxy>:8888`` — host-side egress
    allowlist; NATS is NOT reachable from the container (invariant-9 analog: the spawner owns
    the bus connection and relays).
  * ``--user 1000:1000 --cap-drop=ALL --security-opt no-new-privileges``,
    ``--security-opt seccomp=…``, ``--read-only`` + tmpfs ``/work`` & ``/tmp``, pid/mem/cpu caps.
  * secrets as **0400 bind-mounted files** at ``/run/secrets/{gh_token,bedrock}`` — never
    ``--env-file`` / image layers / container-wide ``-e`` (docs §Lifecycle step 5). The spawner
    NEVER mints tokens; it reads the member's own token from the peer's local secret store.
  * job dir bind-mounted at ``/job``; the runner writes ``events.jsonl`` (live) + ``result.json``
    (atomic-last) there — the host-side relay tails/reads them.

``ContainerLauncher`` is a Protocol so the consumer's launch/liveness logic is unit-testable
against a fake without a live Docker daemon (same "logic in a testable method, thin plumbing"
split the rest of the fleet uses).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

from .config import Settings

logger = logging.getLogger(__name__)

REQUEST_FILENAME = "request.json"


class ContainerLauncher(Protocol):
    async def launch(self, job_id: str, request: dict[str, Any], job_dir: Path) -> str:
        """Write request.json, start the hardened container detached, return its container id."""
        ...

    async def is_alive(self, container_id: str) -> bool:
        """True iff the container is still running."""
        ...

    async def wait(self, container_id: str) -> int:
        """Block until the container exits; return its exit code."""
        ...

    async def remove(self, container_id: str) -> None:
        """Best-effort force-remove (cleanup on relaunch / error)."""
        ...


def build_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a dispatch payload into the runner's ``request.json`` shape.

    v0 (orchestrator decision 1): CLONE inside the container via the member's token — NO cwd
    bind-mount. An OPTIONAL ``owner/repo`` (additive ECA-66 field; mesh path ignores it) drives
    the clone; absent it, the job runs as a pure-reasoning turn in an empty tmpfs workdir.
    """
    request: dict[str, Any] = {
        "job_id": payload["job_id"],
        "prompt": payload.get("prompt", ""),
    }
    if payload.get("model"):
        request["model"] = payload["model"]

    limits = payload.get("limits") or {}
    request["limits"] = {
        # dispatch uses ``wall_clock``; the runner expects ``wall_clock_s``.
        "wall_clock_s": limits.get("wall_clock"),
        "max_turns": limits.get("max_turns"),
        "max_budget_usd": limits.get("max_budget_usd"),
    }

    repo = _resolve_repo(payload)
    if repo is not None:
        request["repo"] = repo
    return request


def _resolve_repo(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Derive the runner ``repo`` block from an OPTIONAL owner/repo (seam for the ECA-66 field).

    Accepts either a structured ``repo`` dict (``{url, ref, clone}``) or a bare ``owner/repo``
    string under ``owner/repo`` / ``owner_repo`` -> ``https://github.com/<owner/repo>.git``.
    """
    if isinstance(payload.get("repo"), dict):
        repo = dict(payload["repo"])
        repo.setdefault("clone", True)
        return repo
    slug = payload.get("owner/repo") or payload.get("owner_repo")
    if isinstance(slug, str) and "/" in slug:
        ref = payload.get("ref")
        repo = {"url": f"https://github.com/{slug}.git", "clone": True}
        if ref:
            repo["ref"] = ref
        return repo
    return None


class DockerLauncher:
    """Concrete launcher shelling out to ``docker``. Replicates smoke.sh's hardened invocation."""

    def __init__(self, settings: Settings):
        self._s = settings

    def _container_name(self, job_id: str) -> str:
        return f"spawner-{job_id}"

    async def launch(self, job_id: str, request: dict[str, Any], job_dir: Path) -> str:
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / REQUEST_FILENAME).write_text(json.dumps(request), encoding="utf-8")
        argv = self._run_argv(job_id, job_dir)
        logger.info("launching container for job %s: %s", job_id, self._s.agent_image)
        out = await self._exec(argv)
        return out.strip()

    def _run_argv(self, job_id: str, job_dir: Path) -> list[str]:
        s = self._s
        proxy_url = f"http://{s.egress_proxy_name}:8888"
        argv = [
            s.docker_bin, "run", "-d", "--rm",
            "--name", self._container_name(job_id),
            "--network", s.egress_network,
            "-e", f"HTTPS_PROXY={proxy_url}", "-e", f"https_proxy={proxy_url}",
            "-e", f"HTTP_PROXY={proxy_url}", "-e", f"http_proxy={proxy_url}",
            "-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1",
            "--user", "1000:1000",
            "--cap-drop=ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/work:rw,size=2g,mode=1777",
            "--tmpfs", "/tmp:rw,size=256m,mode=1777",
            "--pids-limit", "512", "--memory", "4g", "--cpus", "2",
            "-e", "HOME=/work",
            "-v", f"{job_dir}:/job",
        ]
        if s.seccomp_path:
            argv += ["--security-opt", f"seccomp={s.seccomp_path}"]
        # Secrets: 0400 read-only bind mounts; NEVER -e / --env-file (docs §Lifecycle step 5).
        if s.gh_token_path:
            argv += ["-v", f"{s.path(s.gh_token_path)}:/run/secrets/gh_token:ro"]
        if s.bedrock_secret_path:
            argv += ["-v", f"{s.path(s.bedrock_secret_path)}:/run/secrets/bedrock:ro"]
        argv += [s.agent_image, "--job-dir", "/job"]
        return argv

    async def is_alive(self, container_id: str) -> bool:
        try:
            out = await self._exec(
                [self._s.docker_bin, "inspect", "-f", "{{.State.Running}}", container_id]
            )
        except LaunchError:
            return False  # unknown container id -> not alive
        return out.strip() == "true"

    async def wait(self, container_id: str) -> int:
        out = await self._exec([self._s.docker_bin, "wait", container_id])
        try:
            return int(out.strip())
        except ValueError:
            return -1

    async def remove(self, container_id: str) -> None:
        try:
            await self._exec([self._s.docker_bin, "rm", "-f", container_id])
        except LaunchError:
            pass

    async def _exec(self, argv: list[str]) -> str:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise LaunchError(
                f"{' '.join(argv[:3])}… exited {proc.returncode}: "
                f"{stderr.decode('utf-8', 'replace').strip()}"
            )
        return stdout.decode("utf-8", "replace")


class LaunchError(RuntimeError):
    """A docker invocation failed (non-zero exit)."""
