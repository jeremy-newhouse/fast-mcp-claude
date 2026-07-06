"""The control surface (FR-WS7): a unix socket speaking newline-delimited JSON.

Local-only by construction — unix socket, 0600, no network listener, no mesh
verbs (AC-WS-6). The orchestrator drives it via the `workers` CLI over Bash;
its own session permission gate applies there.

Request:  {"verb": "...", "args": {...}}\n
Response: {"ok": true, "data": ...}\n  |  {"ok": false, "error": "..."}\n
`attach` streams one JSON event per line until the client disconnects.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from . import __version__
from .config import Config
from .engine import Engine
from .events import EventLog
from .gate import WorkerPolicy
from .registry import Registry


class ControlServer:
    def __init__(
        self, config: Config, engine: Engine, registry: Registry, events: EventLog
    ) -> None:
        self._cfg = config
        self._engine = engine
        self._reg = registry
        self._events = events
        self._server: asyncio.AbstractServer | None = None

    async def serve_forever(self) -> None:
        path = self._cfg.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(path))
        os.chmod(path, 0o600)
        async with self._server:
            await self._server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw:
                return
            try:
                req = json.loads(raw)
                verb = req.get("verb", "")
                args = req.get("args", {}) or {}
            except json.JSONDecodeError as e:
                await self._reply(writer, {"ok": False, "error": f"bad request: {e}"})
                return

            if verb == "attach":
                await self._attach(writer, args)
                return

            try:
                data = await self._dispatch(verb, args)
                await self._reply(writer, {"ok": True, "data": data})
            except Exception as e:  # noqa: BLE001 — every error becomes a JSON reply
                await self._reply(writer, {"ok": False, "error": str(e)})
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _reply(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        writer.write((json.dumps(payload, default=str) + "\n").encode())
        await writer.drain()

    async def _dispatch(self, verb: str, args: dict[str, Any]) -> Any:
        if verb == "ping":
            return {"version": __version__}
        if verb == "spawn":
            defaults = WorkerPolicy()
            policy = WorkerPolicy(
                allowed_tools=args.get("allowed_tools") or defaults.allowed_tools,
                allow_env=args.get("allow_env", []),
                guard_hooks=args.get("guard_hooks", {}),
                model=args.get("model"),
                limits=args.get("limits", {}),
            )
            worker = await self._engine.spawn(args["name"], args["repo"], policy)
            return {"worker": worker}
        if verb == "prompt":
            turn_id = await self._engine.prompt(args["name"], args["text"])
            return {"turn_id": turn_id}
        if verb == "status":
            return {"workers": await self._engine.status()}
        if verb == "questions":
            rows = await self._reg.pending_questions(args.get("name"))
            for r in rows:
                r["questions"] = json.loads(r["questions"])
            return {"questions": rows}
        if verb == "answer":
            ok = await self._engine.answer(int(args["question_id"]), args["text"])
            return {"answered": ok}
        if verb == "cycle":
            turn_id = await self._engine.cycle(args["name"])
            return {"turn_id": turn_id}
        if verb == "kill":
            await self._engine.kill(args["name"])
            return {"killed": args["name"]}
        if verb == "events":
            return {"events": self._events.read(args["name"], limit=args.get("limit", 100))}
        if verb == "history":
            rows = await self._reg.history(args.get("name"), limit=args.get("limit", 50))
            for r in rows:
                # keep the JSON line consumable; full text lives in the registry
                if r.get("prompt") and len(r["prompt"]) > 400:
                    r["prompt"] = r["prompt"][:400] + "..."
                if r.get("result_text") and len(r["result_text"]) > 400:
                    r["result_text"] = r["result_text"][:400] + "..."
            return {"turns": rows}
        if verb == "get":
            turn = await self._reg.get_turn(int(args["turn_id"]))
            if turn is None:
                raise ValueError(f"no such turn: {args['turn_id']}")
            return {"turn": turn}
        raise ValueError(f"unknown verb: {verb}")

    async def _attach(self, writer: asyncio.StreamWriter, args: dict[str, Any]) -> None:
        """Follow a worker's event stream live (Amendment A9) until disconnect."""
        name = args["name"]
        try:
            async for record in self._events.follow(name):
                writer.write((json.dumps(record, default=str) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
