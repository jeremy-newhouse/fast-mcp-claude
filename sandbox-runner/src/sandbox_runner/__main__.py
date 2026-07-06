"""Container entrypoint CLI: read one job spec, (optionally) clone, run it, relay.

Invocation (the spawner runs this as PID 1's child, uid 1000):

    python -m sandbox_runner --job-dir /job

``/job`` is a bind-mounted directory containing ``request.json``:

    {
      "job_id":  "abc123",
      "prompt":  "Summarize the README in three bullets.",
      "repo":    {"url": "https://github.com/owner/repo.git",
                  "ref": "main", "clone": true},
      "model":   "us.anthropic.claude-...",        # optional; else $ANTHROPIC_MODEL
      "limits":  {"wall_clock_s": 1800, "max_turns": 50, "max_budget_usd": 10.0}
    }

Outputs (same dir): ``events.jsonl`` (live) and ``result.json`` (atomic, last).
The process exit code is 0 whenever a terminal ``result.json`` was written — even
for ``timeout``/``budget_exceeded``/``turn_limit``/``error`` job states — because
those are *job* outcomes the spawner reads from the result, not runner crashes.
A non-zero exit means the runner itself failed to produce a result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .gitcreds import DEFAULT_TOKEN_PATH, GitCloneError, clone, token_present
from .limits import Limits
from .result import JobRelay, JobState, build_result
from .runner import run_job, runner_env_from_process

REQUEST_FILENAME = "request.json"
# Where the working tree is checked out (tmpfs-backed; read-only rootfs elsewhere).
DEFAULT_WORKTREE = "/work/repo"
# Cred-free replay model leg (Q4): when set, swap the live SDK query for canned
# messages so the smoke exercises clone/relay/limits/egress/layer without a bearer.
REPLAY_ENV = "SANDBOX_RUNNER_REPLAY"


def _select_query_fn(relay: JobRelay) -> Any:
    """Return the runner's ``query_fn``: live SDK by default, replay when opted in."""
    value = os.environ.get(REPLAY_ENV)
    if not value:
        return None  # run_job falls back to the live SDK `query`
    from .replay import load_replay_query_fn

    relay.emit("lifecycle", phase="replay_leg", source=value)
    return load_replay_query_fn(value)


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{ts} sandbox-runner: {msg}", file=sys.stderr, flush=True)


def _load_request(job_dir: Path) -> dict[str, Any]:
    path = job_dir / REQUEST_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"missing {REQUEST_FILENAME} in job dir {job_dir}")
    with open(path, encoding="utf-8") as fh:
        req = json.load(fh)
    if not isinstance(req, dict):
        raise ValueError(f"{REQUEST_FILENAME} must be a JSON object")
    return req


async def _prepare_worktree(req: dict[str, Any], relay: JobRelay, token_path: str) -> str:
    """Clone the repo if requested; return the cwd the SDK session runs in."""
    repo = req.get("repo") or {}
    if not repo or not repo.get("clone", bool(repo.get("url"))):
        # No clone: run in an empty tmpfs workdir (e.g. a pure-reasoning job).
        cwd = repo.get("worktree") or DEFAULT_WORKTREE
        Path(cwd).mkdir(parents=True, exist_ok=True)
        relay.emit("lifecycle", phase="no_clone", cwd=cwd)
        return cwd
    url = repo["url"]
    dest = repo.get("worktree") or DEFAULT_WORKTREE
    if not token_present(token_path):
        raise GitCloneError(f"repo clone requested but no token mounted at {token_path}")
    relay.emit("lifecycle", phase="clone_start", url=url, ref=repo.get("ref"))
    await clone(
        url,
        dest,
        ref=repo.get("ref"),
        depth=repo.get("depth", 1),
        token_path=token_path,
    )
    relay.emit("lifecycle", phase="clone_done", cwd=dest)
    return dest


async def _run(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    req = _load_request(job_dir)
    job_id = str(req.get("job_id") or job_dir.name)
    relay = JobRelay(job_dir, job_id)
    started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    relay.emit("lifecycle", phase="boot", runner_version=__version__, job_id=job_id)

    try:
        limits = Limits.from_spec(req.get("limits"))
        model = req.get("model") or os.environ.get("ANTHROPIC_MODEL")
        prompt = req.get("prompt")
        if not prompt:
            raise ValueError("request.json missing required 'prompt'")

        cwd = await _prepare_worktree(req, relay, args.token_path)
        query_fn = _select_query_fn(relay)
        run_kwargs: dict[str, Any] = {
            "prompt": prompt,
            "cwd": cwd,
            "limits": limits,
            "relay": relay,
            "model": model,
            "env": runner_env_from_process(),
        }
        if query_fn is not None:
            run_kwargs["query_fn"] = query_fn
        result = await run_job(**run_kwargs)
        _log(f"job {job_id} finished: state={result['state']} cost={result['total_cost_usd']}")
        return 0
    except Exception as exc:  # noqa: BLE001 — convert setup failures into a result frame
        err = f"{type(exc).__name__}: {exc}"
        _log(f"job {job_id} setup/run failure: {err}")
        relay.finalize(
            build_result(
                state=JobState.ERROR,
                total_cost_usd=None,
                num_turns=None,
                usage=None,
                final_text=None,
                started_at=started_at,
                duration_ms=0,
                error=err,
            )
        )
        return 0  # a result WAS written; the spawner reads state=error from it


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sandbox-runner", description=__doc__)
    p.add_argument("--job-dir", required=True, help="bind-mounted job directory")
    p.add_argument(
        "--token-path",
        default=DEFAULT_TOKEN_PATH,
        help=f"path to the mounted GitHub token (default: {DEFAULT_TOKEN_PATH})",
    )
    p.add_argument("--version", action="version", version=f"sandbox-runner {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        rc = asyncio.run(_run(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
