"""Mesh presence for workers (FR-WS9, Amendment A4): every worker announces as
role="worker", interactive=false — never "live-session".

Reconnect-robustness is the point (AC-WS-10): announce failures ESCAPE the
heartbeat loop to the client-rebuild path. The channel sidecar's _presence_loop
(inner try/except swallowing announce errors against a dead client, forever) is
the named anti-pattern; the launcher's _heartbeat_loop is the model. Auth
failures never retry-storm — the mesh locks the endpoint for 60s after 5 bad
bearers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastmcp import Client

from .config import Config
from .events import EventLog
from .registry import Registry

_AUTH_HINTS = ("401", "unauthorized", "403", "forbidden", "invalid api key", "authentication")
_SUPERVISOR_STREAM = "_supervisor"  # pseudo-worker for loop-level events


def _is_auth_error(e: Exception) -> bool:
    text = str(e).lower()
    return any(h in text for h in _AUTH_HINTS)


class Presence:
    def __init__(self, config: Config, registry: Registry, events: EventLog) -> None:
        self._cfg = config
        self._reg = registry
        self._events = events

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.mesh_url and self._cfg.mesh_api_key)

    def _identity(self, worker: dict[str, Any]) -> str:
        repo_base = Path(worker["repo"]).name
        return f"{self._cfg.machine}.{repo_base}.{worker['name']}"

    async def _announce_all(self, client: Client) -> int:
        """One heartbeat: announce every non-gone worker. Exceptions ESCAPE —
        the caller rebuilds the client (this is the AC-WS-10 requirement)."""
        workers = await self._reg.list_workers()
        for w in workers:
            epoch = await self._reg.current_epoch(w["name"])
            summary = f"[worker] {w['name']} {w['status']} (epoch {epoch['seq'] if epoch else '?'})"
            await client.call_tool(
                "announce",
                {
                    "identity": self._identity(w),
                    "summary": summary[:280],
                    "metadata": {
                        "role": "worker",
                        "interactive": False,
                        "supervised": True,
                        "machine": self._cfg.machine,
                        "repo": Path(w["repo"]).name,
                        "cwd": w["repo"],
                        "name": w["name"],
                        "status": w["status"],
                        "epoch": epoch["seq"] if epoch else None,
                    },
                },
            )
        return len(workers)

    async def run(self) -> None:
        if not self.enabled:
            return
        backoff = 1.0
        while True:
            try:
                async with Client(self._cfg.mesh_url, auth=self._cfg.mesh_api_key) as client:
                    backoff = 1.0
                    self._events.emit(_SUPERVISOR_STREAM, "presence_connected")
                    while True:
                        n = await self._announce_all(client)
                        self._events.emit(_SUPERVISOR_STREAM, "presence_beat", workers=n)
                        await asyncio.sleep(self._cfg.announce_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — every failure rebuilds the client
                if _is_auth_error(e):
                    # 5 bad bearers lock the whole endpoint for 60s; wait it out.
                    delay = max(90.0, float(self._cfg.announce_interval_s))
                else:
                    backoff = min(backoff * 2, 60.0)
                    delay = backoff
                self._events.emit(
                    _SUPERVISOR_STREAM, "presence_reconnect",
                    error=str(e), delay_s=delay, auth=_is_auth_error(e),
                )
                await asyncio.sleep(delay)
