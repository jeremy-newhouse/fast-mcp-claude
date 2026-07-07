"""Daemon entrypoint: lazy-fail boot (the eCA __main__ shape) — an unexpected
crash logs, sleeps 30s, retries; never crash-loops under pm2."""

from __future__ import annotations

import asyncio
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

from .config import load_config
from .engine import Engine
from .envbuild import scrub_daemon_env
from .events import EventLog
from .gate import QuestionBridge
from .presence import Presence
from .registry import Registry
from .server import ControlServer


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{ts} worker-supervisor: {msg}", file=sys.stderr, flush=True)


async def _idle_sweep(engine: Engine) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            retired = await engine.maybe_retire_idle()
            if retired:
                _log(f"idle retirement started for: {', '.join(retired)}")
        except Exception as e:  # noqa: BLE001 — the sweep must survive anything
            _log(f"idle sweep error: {e}")


async def _serve() -> None:
    cfg = load_config()
    cfg.home.mkdir(parents=True, exist_ok=True)

    registry = Registry(cfg.db_path)
    await registry.connect()
    stats = await registry.boot_reconcile()
    _log(f"boot reconcile: {stats}")

    events = EventLog(cfg.logs_dir)
    bridge = QuestionBridge(registry, events)
    engine = Engine(cfg, registry, events, bridge)  # snapshots the boot env first
    kept = scrub_daemon_env()  # then harden the daemon env (AC-WS-9, Amendment A7)
    _log(f"daemon env scrubbed to minimal base: {', '.join(sorted(kept))}")

    await engine.start()
    presence = Presence(cfg, registry, events)
    _log(f"mesh presence: {'enabled' if presence.enabled else 'disabled'}")

    background = [
        asyncio.create_task(presence.run(), name="presence"),
        asyncio.create_task(_idle_sweep(engine), name="idle-sweep"),
    ]
    server = ControlServer(cfg, engine, registry, events)
    # AC#5 (ECA-72): refuse to boot if a live daemon already holds the socket.
    # A second instance would steal it from pm2 silently (real incident 2026-07-07).
    try:
        await server.preflight_socket_check()
    except SystemExit:
        _log(
            f"FATAL: control socket {cfg.socket_path} already has a live listener — "
            "refusing to boot to avoid stealing it from the running daemon; "
            "stop the existing instance first."
        )
        raise
    server_task = asyncio.create_task(server.serve_forever(), name="control")
    _log(f"control socket: {cfg.socket_path}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    _log("shutting down")
    for t in (server_task, *background):
        t.cancel()
    for t in (server_task, *background):
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    await engine.stop()
    await registry.close()


def main() -> None:
    # AC#6 (ECA-72): operators running `worker-supervisor status` by hand must not
    # accidentally boot a daemon. Any unexpected argument is a usage error.
    if len(sys.argv) > 1:
        print(
            f"worker-supervisor: unexpected arguments: {sys.argv[1:]!r}\n"
            "This command starts the supervisor daemon. "
            "To manage workers, use the `workers` CLI instead.",
            file=sys.stderr,
        )
        sys.exit(2)
    while True:
        try:
            asyncio.run(_serve())
            return  # clean shutdown (SIGTERM/SIGINT)
        except KeyboardInterrupt:
            return
        except Exception:  # noqa: BLE001 — lazy-fail boot
            _log("unexpected crash:\n" + traceback.format_exc())
            _log("retrying in 30s")
            time.sleep(30)


if __name__ == "__main__":
    main()
