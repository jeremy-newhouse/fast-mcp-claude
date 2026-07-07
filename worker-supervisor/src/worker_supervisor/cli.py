"""The `workers` CLI (FR-WS1): a thin JSON client over the control socket.

Every command prints one JSON document — the orchestrator parses, humans read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from .config import load_config


async def _call(verb: str, args: dict[str, Any], *, stream: bool = False) -> int:
    cfg = load_config()
    try:
        reader, writer = await asyncio.open_unix_connection(str(cfg.socket_path))
    except (FileNotFoundError, ConnectionRefusedError):
        print(
            json.dumps(
                {"ok": False, "error": f"supervisor not running (socket {cfg.socket_path})"}
            )
        )
        return 2
    writer.write((json.dumps({"verb": verb, "args": args}) + "\n").encode())
    await writer.drain()
    code = 0
    try:
        if stream:
            while True:
                line = await reader.readline()
                if not line:
                    break
                sys.stdout.write(line.decode())
                sys.stdout.flush()
        else:
            line = await reader.readline()
            resp = json.loads(line) if line else {"ok": False, "error": "no response"}
            print(json.dumps(resp, indent=2, default=str))
            code = 0 if resp.get("ok") else 1
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    return code


def _parse_limits(ns: argparse.Namespace) -> dict[str, Any]:
    limits: dict[str, Any] = {}
    if ns.wall_clock is not None:
        limits["wall_clock_s"] = ns.wall_clock
    if ns.max_turns is not None:
        limits["max_turns"] = ns.max_turns
    if ns.budget is not None:
        limits["max_budget_usd_per_epoch"] = ns.budget
    return limits


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="workers", description="worker-supervisor control CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn", help="create a worker (idle, epoch 1)")
    sp.add_argument("name")
    sp.add_argument("repo", help="worker cwd root (the cwd pin)")
    sp.add_argument("--tools", help="comma-separated tool ceiling specs, e.g. 'Read,Bash(uv run*)'")
    sp.add_argument("--allow-env", action="append", default=[], metavar="NAME")
    sp.add_argument("--guard-hook", action="append", default=[], metavar="TOOL=script.sh")
    sp.add_argument("--model")
    sp.add_argument("--wall-clock", type=int, default=None, metavar="S")
    sp.add_argument("--max-turns", type=int, default=None)
    sp.add_argument("--budget", type=float, default=None, metavar="USD")

    pp = sub.add_parser("prompt", help="enqueue a turn")
    pp.add_argument("name")
    pp.add_argument("text")

    sub.add_parser("status", help="per-worker state, context pressure, cost")

    qp = sub.add_parser("questions", help="pending AskUserQuestion escalations")
    qp.add_argument("name", nargs="?")

    ap = sub.add_parser("answer", help="answer a parked question")
    ap.add_argument("question_id", type=int)
    ap.add_argument("text")

    cp = sub.add_parser("cycle", help="handover-write, roll epoch, handover-restore")
    cp.add_argument("name")

    kp = sub.add_parser("kill", help="terminate a worker (registry + logs retained)")
    kp.add_argument("name")

    ep = sub.add_parser("events", help="a worker's recorded events")
    ep.add_argument("name")
    ep.add_argument("--limit", type=int, default=100)

    tp = sub.add_parser("attach", help="follow a worker's event stream live")
    tp.add_argument("name")

    hp = sub.add_parser("history", help="recorded turns, newest first")
    hp.add_argument("name", nargs="?")
    hp.add_argument("--limit", type=int, default=50)

    gp = sub.add_parser("get", help="one turn's full record")
    gp.add_argument("turn_id", type=int)

    sub.add_parser("ping", help="daemon liveness + version")

    ns = p.parse_args(argv)

    if ns.cmd == "spawn":
        args: dict[str, Any] = {"name": ns.name, "repo": ns.repo}
        if ns.tools:
            args["allowed_tools"] = [t.strip() for t in ns.tools.split(",") if t.strip()]
        if ns.allow_env:
            args["allow_env"] = ns.allow_env
        if ns.guard_hook:
            hooks = {}
            for spec in ns.guard_hook:
                tool, _, script = spec.partition("=")
                if not script:
                    p.error(f"--guard-hook needs TOOL=script.sh, got {spec!r}")
                hooks[tool] = script
            args["guard_hooks"] = hooks
        if ns.model:
            args["model"] = ns.model
        limits = _parse_limits(ns)
        if limits:
            args["limits"] = limits
        rc = asyncio.run(_call("spawn", args))
    elif ns.cmd == "prompt":
        rc = asyncio.run(_call("prompt", {"name": ns.name, "text": ns.text}))
    elif ns.cmd == "status":
        rc = asyncio.run(_call("status", {}))
    elif ns.cmd == "questions":
        rc = asyncio.run(_call("questions", {"name": ns.name} if ns.name else {}))
    elif ns.cmd == "answer":
        rc = asyncio.run(_call("answer", {"question_id": ns.question_id, "text": ns.text}))
    elif ns.cmd == "cycle":
        rc = asyncio.run(_call("cycle", {"name": ns.name}))
    elif ns.cmd == "kill":
        rc = asyncio.run(_call("kill", {"name": ns.name}))
    elif ns.cmd == "events":
        rc = asyncio.run(_call("events", {"name": ns.name, "limit": ns.limit}))
    elif ns.cmd == "attach":
        try:
            rc = asyncio.run(_call("attach", {"name": ns.name}, stream=True))
        except KeyboardInterrupt:
            rc = 0
    elif ns.cmd == "history":
        args = {"limit": ns.limit}
        if ns.name:
            args["name"] = ns.name
        rc = asyncio.run(_call("history", args))
    elif ns.cmd == "get":
        rc = asyncio.run(_call("get", {"turn_id": ns.turn_id}))
    else:  # ping
        rc = asyncio.run(_call("ping", {}))
    sys.exit(rc)


if __name__ == "__main__":
    main()
