"""The turn engine (FR-WS1/2/6/11): per-worker FIFO, one running turn per worker,
per-turn query()+resume epochs, the agent.py failure ladder adapted.

Concurrency shape (daemon.py's, generalized): one loop task per worker serializes
its turns; workers run concurrently under a global semaphore. Per-turn one-shot
`query()` means no kept-alive client and no task-affinity constraint — a turn is
born and dies inside one coroutine.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ProcessError,
    ResultMessage,
    ToolUseBlock,
    query,
)

from .capsule import write_capsule
from .config import Config
from .envbuild import build_worker_env, snapshot_boot_env
from .events import EventLog
from .gate import QuestionBridge, WorkerPolicy, make_gate, make_question_hook
from .registry import Registry, WORKER_GONE

# Nominal context window for pressure estimation (tokens). The SDK's ResultMessage
# usage reports the last request's input/cache token counts, which approximate the
# session's context size; query() exposes no direct context-fill signal.
CONTEXT_WINDOW_TOKENS = 200_000

CYCLE_PROMPT = (
    "You are being cycled to a fresh context window. Write your session handover NOW: "
    "use `/handover write` if this repo has the handover skill, otherwise write "
    ".claude/handovers/HANDOVER-<utc-date>-<topic>.md per repo convention. Capture task "
    "state, next steps, critical context/traps, and failed approaches. Then stop; do not "
    "start new work."
)
RESTORE_PROMPT = (
    "You are a fresh context taking over from your previous epoch. Restore your working "
    "state: run `/handover restore` if this repo has the handover skill; otherwise read "
    "the newest file in .claude/handovers/, treat it as your session handover, and "
    "continue its next steps."
)
RETIRE_PROMPT = (
    "You are being retired after an idle period. Write a final session handover NOW "
    "(same conventions as /handover write) recording where the work stands for a "
    "successor. Then stop."
)


def session_transcript_path(cwd: str, session_id: str) -> Path:
    """The CLI's cwd-keyed session store: ~/.claude/projects/<sanitized-cwd>/<sid>.jsonl."""
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / sanitized / f"{session_id}.jsonl"


async def _prompt_as_stream(prompt: str) -> AsyncIterator[dict[str, Any]]:
    """can_use_tool requires streaming input (G1) — single-message stream."""
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
    }


@dataclass
class TurnOutcome:
    session_id: str | None = None
    result_text: str | None = None
    is_error: bool = False
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    usage: dict[str, Any] | None = None
    tools: list[str] = field(default_factory=list)
    saw_result: bool = False


def context_pressure_pct(usage: dict[str, Any] | None) -> int | None:
    if not usage:
        return None
    ctx = (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )
    if ctx <= 0:
        return None
    return min(100, round(100 * ctx / CONTEXT_WINDOW_TOKENS))


class Engine:
    def __init__(
        self,
        config: Config,
        registry: Registry,
        events: EventLog,
        bridge: QuestionBridge,
    ) -> None:
        self._cfg = config
        self._reg = registry
        self._events = events
        self._bridge = bridge
        self._boot_env = snapshot_boot_env()
        self._sem = asyncio.Semaphore(config.max_concurrent_turns)
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._kicks: dict[str, asyncio.Event] = {}
        self._current: dict[str, asyncio.Task[None]] = {}
        self._watchdogs: set[asyncio.Task[None]] = set()

    # -- lifecycle verbs (the control surface calls these) ---------------------

    async def start(self) -> None:
        """Arm runners for every persisted active worker (boot recovery path)."""
        for w in await self._reg.list_workers():
            self._ensure_runner(w["name"])

    async def stop(self) -> None:
        for task in [*self._runners.values(), *self._watchdogs]:
            task.cancel()
        for task in [*self._runners.values(), *self._watchdogs]:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._runners.clear()
        self._watchdogs.clear()

    async def spawn(
        self, name: str, repo: str, policy: WorkerPolicy | None = None
    ) -> dict[str, Any]:
        repo_path = Path(repo).expanduser().resolve(strict=True)
        if not repo_path.is_dir():
            raise ValueError(f"repo is not a directory: {repo}")
        policy = policy or WorkerPolicy()
        worker = await self._reg.spawn_worker(name, str(repo_path), json.loads(policy.to_json()))
        self._events.emit(name, "worker_spawned", repo=str(repo_path))
        self._ensure_runner(name)
        return worker

    async def prompt(self, name: str, text: str) -> int:
        worker = await self._require_active(name)
        turn_id = await self._reg.enqueue_turn(worker["name"], text, kind="prompt")
        self._events.emit(name, "turn_enqueued", turn_id=turn_id, kind="prompt")
        self._kick(name)
        return turn_id

    async def cycle(self, name: str) -> int:
        """Manual cycle: handover-write turn; epoch rolls when it completes."""
        worker = await self._require_active(name)
        turn_id = await self._reg.enqueue_turn(worker["name"], CYCLE_PROMPT, kind="cycle_handover")
        self._events.emit(name, "cycle_requested", turn_id=turn_id)
        self._kick(name)
        return turn_id

    async def answer(self, question_id: int, text: str) -> bool:
        return await self._bridge.answer(question_id, text)

    async def kill(self, name: str) -> None:
        """Terminate the worker: cancel any running turn (SDK close escalates
        SIGTERM->SIGKILL on the subprocess), finish records, retain registry+logs."""
        worker = await self._reg.get_worker(name)
        if worker is None:
            raise ValueError(f"no such worker: {name}")
        await self._reg.set_worker_status(name, "killed")
        task = self._current.get(name)
        if task is not None and not task.done():
            task.cancel()
        # Finish whatever turn was active; CAS makes double-finish harmless.
        for t in await self._reg.history(name, limit=5):
            if t["state"] in ("claimed", "running"):
                await self._reg.finish_turn(t["id"], "killed", error="worker killed")
        epoch = await self._reg.current_epoch(name)
        if epoch is not None and epoch.get("ended_at") is None:
            await self._reg.end_epoch(epoch["id"], "killed")
        self._events.emit(name, "worker_killed")
        self._kick(name)

    async def maybe_retire_idle(self) -> list[str]:
        """Idle-retirement sweep (Amendment A8): enqueue a final handover-write
        turn for workers idle past the timeout; retirement completes in _after_turn."""
        from datetime import datetime, timezone

        retired: list[str] = []
        for w in await self._reg.list_workers():
            if w["status"] != "idle":
                continue
            if await self._reg.next_queued_turn(w["name"]):
                continue
            last = datetime.fromisoformat(w["last_active_at"])
            idle_s = (datetime.now(timezone.utc) - last).total_seconds()
            if idle_s < self._cfg.idle_timeout_s:
                continue
            last_turn = await self._reg.last_finished_turn(w["name"])
            if last_turn is not None and last_turn["kind"] == "retire_handover":
                continue  # retirement already in flight/failed; don't loop
            await self._reg.enqueue_turn(w["name"], RETIRE_PROMPT, kind="retire_handover")
            self._events.emit(w["name"], "idle_retirement_started", idle_s=int(idle_s))
            self._kick(w["name"])
            retired.append(w["name"])
        return retired

    # -- internals ---------------------------------------------------------------

    def _kick(self, name: str) -> None:
        self._kicks.setdefault(name, asyncio.Event()).set()

    def _ensure_runner(self, name: str) -> None:
        task = self._runners.get(name)
        if task is None or task.done():
            self._kicks.setdefault(name, asyncio.Event())
            self._runners[name] = asyncio.create_task(
                self._worker_loop(name), name=f"worker-{name}"
            )

    async def _require_active(self, name: str) -> dict[str, Any]:
        worker = await self._reg.get_worker(name)
        if worker is None:
            raise ValueError(f"no such worker: {name}")
        if worker["status"] in WORKER_GONE:
            raise ValueError(f"worker {name!r} is {worker['status']}")
        self._ensure_runner(name)
        return worker

    async def _worker_loop(self, name: str) -> None:
        kick = self._kicks[name]
        while True:
            worker = await self._reg.get_worker(name)
            if worker is None or worker["status"] in WORKER_GONE:
                return
            turn = await self._reg.next_queued_turn(name)
            if turn is None:
                kick.clear()
                try:
                    await asyncio.wait_for(kick.wait(), timeout=15)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                continue
            if not await self._reg.claim_turn(turn["id"]):
                continue  # someone raced us; re-scan
            async with self._sem:
                task = asyncio.create_task(self._run_turn(name, turn["id"]))
                self._current[name] = task
                try:
                    await task
                except asyncio.CancelledError:
                    if task.cancelled():
                        continue  # the turn was killed; loop decides via status
                    task.cancel()
                    raise
                finally:
                    self._current.pop(name, None)
            await self._after_turn(name, turn["id"])

    async def _run_turn(self, name: str, turn_id: int) -> None:
        worker = await self._reg.get_worker(name)
        turn = await self._reg.get_turn(turn_id)
        assert worker is not None and turn is not None
        policy = WorkerPolicy.from_json(
            worker["policy"] if isinstance(worker["policy"], str) else json.dumps(worker["policy"])
        )
        limits = self._cfg.limits.override(policy.limits)
        epoch = await self._reg.current_epoch(name)
        assert epoch is not None

        # Budget gate, pre-spawn (AC-WS-5): a breached epoch refuses new turns.
        remaining_budget = limits.max_budget_usd_per_epoch - (epoch["cost_usd"] or 0.0)
        if remaining_budget <= 0:
            await self._reg.finish_turn(
                turn_id, "budget_refused",
                error=f"epoch budget exhausted (cap {limits.max_budget_usd_per_epoch} USD)",
            )
            self._events.emit(name, "turn_budget_refused", turn_id=turn_id)
            await self._finish_failure_capsule(name, turn_id, "budget_refused", {}, [], [])
            await self._reg.set_worker_status(name, "idle")
            return

        resume_from = await self._pick_resume_target(name, worker["repo"], epoch["id"], turn_id)
        stderr_tail: deque[str] = deque(maxlen=200)
        await self._reg.set_worker_status(name, "running", active=True)

        options_snapshot = {
            "cwd": worker["repo"],
            "resume": resume_from,
            "setting_sources": ["project"],
            "tools": policy.base_tools(),
            "allowed_tools": policy.allowed_tools,
            "max_turns": limits.max_turns,
            "max_budget_usd": round(remaining_budget, 4),
            "model": policy.model,
            "allow_env": policy.allow_env,
            "wall_clock_s": limits.wall_clock_s,
        }

        attempt = 0
        while True:
            attempt += 1
            await self._reg.start_turn(turn_id, resume_from)
            self._events.emit(
                name, "turn_started", turn_id=turn_id, kind=turn["kind"],
                attempt=attempt, resume=resume_from,
            )
            gate = make_gate(
                worker=name,
                repo_root=Path(worker["repo"]),
                policy=policy,
                bridge=self._bridge,
                events=self._events,
                turn_id=turn_id,
                question_timeout_s=self._cfg.question_timeout_s,
            )
            options = ClaudeAgentOptions(
                cwd=worker["repo"],
                resume=resume_from,
                setting_sources=["project"],
                tools=policy.base_tools(),
                # ADR-0005 shape: NOTHING is pre-approved, so every privileged
                # call routes through the gate. Pre-approving AskUserQuestion
                # would bypass can_use_tool and the tool errors headless.
                allowed_tools=[],
                max_turns=limits.max_turns,
                max_budget_usd=max(0.01, round(remaining_budget, 4)),
                model=policy.model,
                env=build_worker_env(
                    self._boot_env,
                    policy.allow_env,
                    mcp_tool_timeout_ms=(self._cfg.question_timeout_s + 300) * 1000,
                ),
                can_use_tool=gate,
                # AskUserQuestion never reaches can_use_tool (UI tool) — the
                # bridge intercepts it as a PreToolUse hook. The matcher timeout
                # must outlive the question timeout or the CLI kills the park.
                hooks={
                    "PreToolUse": [
                        HookMatcher(
                            matcher="AskUserQuestion",
                            hooks=[
                                make_question_hook(
                                    worker=name,
                                    turn_id=turn_id,
                                    bridge=self._bridge,
                                    question_timeout_s=self._cfg.question_timeout_s,
                                )
                            ],
                            timeout=self._cfg.question_timeout_s + 120,
                        )
                    ]
                },
                stderr=stderr_tail.append,
            )
            outcome = TurnOutcome()
            try:
                async with asyncio.timeout(limits.wall_clock_s):
                    async for msg in query(
                        prompt=_prompt_as_stream(turn["prompt"]), options=options
                    ):
                        self._observe(name, turn_id, msg, outcome)
                break  # stream completed
            except (asyncio.TimeoutError, TimeoutError):
                # Wall-clock breach: cancellation closed the transport, which
                # escalates SIGTERM->SIGKILL on the subprocess group.
                await self._reg.finish_turn(
                    turn_id, "timeout",
                    session_id=outcome.session_id,
                    error=f"wall clock exceeded ({limits.wall_clock_s}s)",
                    tools=outcome.tools,
                )
                self._events.emit(name, "turn_timeout", turn_id=turn_id)
                await self._finish_failure_capsule(
                    name, turn_id, "timeout", options_snapshot, list(stderr_tail), [resume_from]
                )
                await self._reg.set_worker_status(name, "idle", active=True)
                return
            except ProcessError as e:
                if resume_from is not None:
                    # G7: the chain is dead. Never silently fresh — end the epoch,
                    # open the next one grounded on the handover file.
                    await self._reg.finish_turn(
                        turn_id, "error",
                        error=f"resume failed: {e} (exit={e.exit_code})",
                    )
                    self._events.emit(
                        name, "resume_failed", turn_id=turn_id, resume=resume_from
                    )
                    await self._finish_failure_capsule(
                        name, turn_id, "resume_failed", options_snapshot,
                        list(stderr_tail), [resume_from],
                    )
                    await self._reg.roll_epoch(name, "resume_failed")
                    await self._reg.enqueue_turn(name, RESTORE_PROMPT, kind="restore")
                    await self._reg.set_worker_status(name, "idle", active=True)
                    self._kick(name)
                    return
                if attempt == 1:
                    self._events.emit(name, "turn_retry", turn_id=turn_id, error=str(e))
                    continue
                await self._fail_turn(
                    name, turn_id, outcome, f"ProcessError: {e}", options_snapshot,
                    stderr_tail, resume_from,
                )
                return
            except asyncio.CancelledError:
                raise  # kill() owns the record
            except Exception as e:  # noqa: BLE001 — G2: mid-stream death is a BARE Exception
                if attempt == 1:
                    self._events.emit(name, "turn_retry", turn_id=turn_id, error=str(e))
                    continue
                await self._fail_turn(
                    name, turn_id, outcome, f"{type(e).__name__}: {e}", options_snapshot,
                    stderr_tail, resume_from,
                )
                return

        # Stream completed. Question timeout ends the stream via deny+interrupt —
        # classify it distinctly (the question row was CAS'd to timed_out).
        timed_out_q = [
            q for q in await self._question_states(turn_id) if q["state"] == "timed_out"
        ]
        if timed_out_q:
            await self._reg.finish_turn(
                turn_id, "question_timeout",
                session_id=outcome.session_id,
                result_text=outcome.result_text,
                cost_usd=outcome.cost_usd,
                duration_ms=outcome.duration_ms,
                num_turns=outcome.num_turns,
                usage=outcome.usage,
                tools=outcome.tools,
                error="question timed out unanswered",
            )
            self._events.emit(name, "turn_question_timeout", turn_id=turn_id)
            await self._finish_failure_capsule(
                name, turn_id, "question_timeout", options_snapshot,
                list(stderr_tail), [resume_from, outcome.session_id],
            )
            await self._reg.set_worker_status(name, "idle", active=True)
            return

        if not outcome.saw_result:
            await self._fail_turn(
                name, turn_id, outcome, "stream ended without a ResultMessage",
                options_snapshot, stderr_tail, resume_from,
            )
            return

        state = "error" if outcome.is_error else "done"
        # G4: session id + telemetry persist atomically with the terminal state,
        # BEFORE anyone can observe the turn as finished.
        await self._reg.finish_turn(
            turn_id, state,
            session_id=outcome.session_id,
            result_text=outcome.result_text,
            is_error=outcome.is_error,
            cost_usd=outcome.cost_usd,
            duration_ms=outcome.duration_ms,
            num_turns=outcome.num_turns,
            usage=outcome.usage,
            tools=outcome.tools,
        )
        self._events.emit(
            name, "turn_finished", turn_id=turn_id, state=state,
            session_id=outcome.session_id, cost_usd=outcome.cost_usd,
            duration_ms=outcome.duration_ms, num_turns=outcome.num_turns,
            context_pct=context_pressure_pct(outcome.usage),
        )
        if outcome.session_id:
            watchdog = asyncio.create_task(
                self._verify_transcript_persisted(
                    name, worker["repo"], outcome.session_id, turn_id
                )
            )
            self._watchdogs.add(watchdog)
            watchdog.add_done_callback(self._watchdogs.discard)
        if state == "error":
            await self._finish_failure_capsule(
                name, turn_id, "result_error", options_snapshot,
                list(stderr_tail), [resume_from, outcome.session_id],
            )
        await self._reg.set_worker_status(name, "idle", active=True)

    def _transcript_exists(self, cwd: str, session_id: str) -> bool:
        return session_transcript_path(cwd, session_id).exists()

    async def _pick_resume_target(
        self, name: str, cwd: str, epoch_id: int, turn_id: int
    ) -> str | None:
        """Newest session id in the epoch whose transcript is actually on disk.

        CLI 2.1.165 (SDK mode) writes the transcript at process exit, and the
        SDK's close() (stdin-close -> 5s -> SIGTERM -> SIGKILL) races it: a turn
        can report a session id that never persists. Resuming that id fails with
        'No conversation found'. Skipping to the newest persisted id loses one
        turn of context instead of the whole epoch; G7 remains the backstop.
        """
        cur = await self._reg.db.execute(
            "SELECT DISTINCT session_id FROM turns WHERE epoch_id = ?"
            " AND session_id IS NOT NULL ORDER BY id DESC",
            (epoch_id,),
        )
        sids = [r["session_id"] for r in await cur.fetchall()]
        for i, sid in enumerate(sids):
            if self._transcript_exists(cwd, sid):
                if i > 0:
                    self._events.emit(
                        name, "resume_target_skipped", turn_id=turn_id,
                        missing=sids[:i], resumed=sid,
                    )
                return sid
        if sids:
            self._events.emit(
                name, "resume_target_skipped", turn_id=turn_id, missing=sids, resumed=None
            )
        return None

    async def _verify_transcript_persisted(
        self, name: str, cwd: str, session_id: str, turn_id: int
    ) -> None:
        """Post-turn watchdog: wait briefly for the transcript, then warn."""
        for _ in range(16):
            if self._transcript_exists(cwd, session_id):
                return
            await asyncio.sleep(0.5)
        self._events.emit(
            name, "session_transcript_missing", turn_id=turn_id, session_id=session_id
        )

    async def _fail_turn(
        self,
        name: str,
        turn_id: int,
        outcome: TurnOutcome,
        error: str,
        options_snapshot: dict[str, Any],
        stderr_tail: deque[str],
        resume_from: str | None,
    ) -> None:
        """Terminal error after the retry: record, capsule, keep the epoch
        (keep-on-failure, Amendment A6) — the orchestrator decides what's next."""
        await self._reg.finish_turn(
            turn_id, "error",
            session_id=outcome.session_id,
            cost_usd=outcome.cost_usd,
            duration_ms=outcome.duration_ms,
            usage=outcome.usage,
            tools=outcome.tools,
            error=error,
        )
        self._events.emit(name, "turn_error", turn_id=turn_id, error=error)
        await self._finish_failure_capsule(
            name, turn_id, "error", options_snapshot, list(stderr_tail),
            [resume_from, outcome.session_id],
        )
        await self._reg.set_worker_status(name, "idle", active=True)

    async def _finish_failure_capsule(
        self,
        name: str,
        turn_id: int,
        reason: str,
        options_snapshot: dict[str, Any],
        stderr_tail: list[str],
        resume_chain: list[str | None],
    ) -> None:
        try:
            turn = await self._reg.get_turn(turn_id)
            path = write_capsule(
                self._cfg.capsules_dir,
                worker=name,
                turn=turn or {"id": turn_id},
                reason=reason,
                options_snapshot=options_snapshot,
                events_tail=self._events.read(name, limit=50),
                stderr_tail=stderr_tail,
                resume_chain=[s for s in resume_chain],
            )
            self._events.emit(name, "failure_capsule", turn_id=turn_id, path=str(path))
        except Exception as e:  # noqa: BLE001 — capsule failure is never fatal
            self._events.emit(name, "failure_capsule_error", turn_id=turn_id, error=str(e))

    async def _question_states(self, turn_id: int) -> list[dict[str, Any]]:
        cur = await self._reg.db.execute(
            "SELECT * FROM questions WHERE turn_id = ?", (turn_id,)
        )
        return [dict(r) for r in await cur.fetchall()]

    def _observe(self, name: str, turn_id: int, msg: Any, outcome: TurnOutcome) -> None:
        if isinstance(msg, ResultMessage):
            outcome.saw_result = True
            outcome.session_id = msg.session_id
            outcome.result_text = msg.result
            outcome.is_error = bool(msg.is_error)
            outcome.cost_usd = msg.total_cost_usd
            outcome.duration_ms = msg.duration_ms
            outcome.num_turns = msg.num_turns
            outcome.usage = msg.usage
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    outcome.tools.append(block.name)
                    self._events.emit(
                        name, "tool_use", turn_id=turn_id, tool=block.name
                    )

    async def _after_turn(self, name: str, turn_id: int) -> None:
        """Lifecycle chaining once a turn reaches a terminal state."""
        turn = await self._reg.get_turn(turn_id)
        if turn is None or turn["state"] != "done":
            return  # keep-on-failure: no auto-progression past a failed turn
        kind = turn["kind"]
        if kind == "cycle_handover":
            epoch = await self._reg.roll_epoch(name, "cycled")
            await self._reg.enqueue_turn(name, RESTORE_PROMPT, kind="restore")
            self._events.emit(name, "epoch_cycled", new_epoch=epoch["seq"])
            self._kick(name)
            return
        if kind == "retire_handover":
            epoch = await self._reg.current_epoch(name)
            if epoch is not None:
                await self._reg.end_epoch(epoch["id"], "idle_retired")
            await self._reg.set_worker_status(name, "retired")
            self._events.emit(name, "worker_retired")
            self._kick(name)  # loop observes retired and exits
            return
        # Auto-cycle on context pressure (FR-WS6/ECA-49), only off a clean turn
        # with an empty queue (never stack cycles behind pending work).
        usage = json.loads(turn["usage"]) if turn.get("usage") else None
        pct = context_pressure_pct(usage)
        if (
            kind == "prompt"
            and pct is not None
            and pct >= self._cfg.cycle_context_pct
            and await self._reg.next_queued_turn(name) is None
        ):
            self._events.emit(name, "auto_cycle", context_pct=pct)
            await self._reg.enqueue_turn(name, CYCLE_PROMPT, kind="cycle_handover")
            self._kick(name)

    # -- status (FR-WS6) --------------------------------------------------------

    async def status(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for w in await self._reg.list_workers(include_gone=True):
            name = w["name"]
            epoch = await self._reg.current_epoch(name)
            last = await self._reg.last_finished_turn(name)
            usage = json.loads(last["usage"]) if last and last.get("usage") else None
            cur = await self._reg.db.execute(
                "SELECT state, COUNT(*) AS n FROM turns WHERE worker = ? GROUP BY state",
                (name,),
            )
            counts = {r["state"]: r["n"] for r in await cur.fetchall()}
            pending_q = await self._reg.pending_questions(name)
            out.append(
                {
                    "name": name,
                    "status": w["status"],
                    "repo": w["repo"],
                    "epoch": epoch["seq"] if epoch else None,
                    "epoch_cost_usd": round(epoch["cost_usd"], 4) if epoch else None,
                    "turns": counts,
                    "last_turn_state": last["state"] if last else None,
                    "context_pct": context_pressure_pct(usage),
                    "pending_questions": len(pending_q),
                    "last_active_at": w["last_active_at"],
                }
            )
        return out
