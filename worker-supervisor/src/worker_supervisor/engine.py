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
import os
import re
import secrets
from collections import deque
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ProcessError,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
    query,
)

from .capsule import write_capsule
from .config import Config, Limits
from .envbuild import build_worker_env, snapshot_boot_env
from .events import EventLog
from .gate import (
    QuestionBridge,
    WorkerPolicy,
    make_gate,
    make_policy_hook,
    make_question_hook,
)
from .names import DAEMON_KEY, require_safe_worker_name
from .registry import Registry, TURN_TERMINAL, WORKER_GONE

# ECA-137: the validator and its pattern moved to `names.py` so that `events.py` and
# `capsule.py` — which this module imports, and which derive filenames from a name too
# — can hold the SAME guard without an import cycle. The refusal MESSAGE is byte-identical
# to ECA-135's; the behaviour is not quite ("only the home moved" was a review finding) —
# a non-str name now raises ValueError where it used to raise TypeError out of
# `re.fullmatch`, which is deliberate. See `names.is_safe_worker_name`.

# Nominal context window for pressure estimation (tokens). Context size is read
# from the LAST AssistantMessage's per-request usage; ResultMessage.usage is the
# SUM across the turn's API calls (a 7-call turn reported cache_read 322k > the
# whole window, proven live) and only serves as a fallback when no assistant
# usage was seen. query() exposes no direct context-fill signal.
CONTEXT_WINDOW_TOKENS = 200_000

# Lifecycle (handover/restore/retire) turns must run even in a budget-exhausted
# epoch, or a lane that hit its cap can never cycle or retire out — the cycle's own
# handover-write turn is enqueued into the exhausted epoch and would be refused,
# so the epoch never rolls and the lane wedges forever (ECA-99 self-cycle deadlock).
# They are exempted from the pre-spawn budget gate and given a reserved SDK budget
# floor so a real `/handover write` isn't clamped to the $0.01 no-op floor.
LIFECYCLE_KINDS = frozenset({"cycle_handover", "restore", "retire_handover"})
LIFECYCLE_BUDGET_RESERVE_USD = 5.0

# ECA-143: the CLI's own `errors` list, bounded before it reaches a capsule. A
# diagnostic is worth nothing if one pathological entry makes the evidence bundle
# unreadable, and the point of the capsule is that a human can open it.
MAX_ERROR_ITEMS = 10
ERROR_ITEM_CHARS = 500

# ECA-148: how long `stop()` waits for cancelled runners/watchdogs before it gives
# up on them and says so. A backstop for an await cancellation cannot reach (the
# SDK's shielded, explicitly unbounded `Query.close()`), not a tuning knob — so it
# is a constant rather than a config key.
#
# Sized ABOVE the SDK's own worst case, not below it: `SubprocessCLITransport.close()`
# documents "every await in this scope is bounded (~20s worst case)" — 5s for the
# write lock, then 5s each for the graceful wait, the post-SIGTERM wait and the
# post-SIGKILL wait. A grace under that would abandon a teardown that was about to
# finish on its own, and `stop_incomplete` would then mean "slow" rather than
# "wedged", which is the one thing it must not mean. Review round: it was 10.0.
# Under pm2 the effective ceiling is `kill_timeout` (unset -> 1600ms default)
# regardless; this grace is for a hand-run daemon and for tests.
STOP_GRACE_S = 25.0

# How long a stopping runner lets `_after_turn` finish. It chains lifecycle state
# across several awaits and is a handful of DB writes, so it is worth waiting out;
# see the call site for why cancelling halfway is unrecoverable.
AFTER_TURN_GRACE_S = 5.0


def _self_cancelled() -> bool:
    """Has cancellation been requested on the CURRENTLY RUNNING task?

    `Task.cancel()` increments this counter on its target before it touches the
    future that task is awaiting — so it distinguishes "I was cancelled" from "the
    thing I was awaiting was cancelled", which is the distinction `_worker_loop`
    could not make and ECA-148 turned into a shutdown that never returned.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0

# Lifecycle prompts embed the ABSOLUTE handover dir: a weak model given a bare
# ".claude/handovers/" resolved it against $HOME, missed the repo's handover,
# and restored as a fresh start (proven live on haiku).
def cycle_prompt(repo: str) -> str:
    return (
        "You are being cycled to a fresh context window. Write your session handover NOW: "
        "use `/handover write` if this repo has the handover skill, otherwise write "
        f"{repo}/.claude/handovers/HANDOVER-<utc-date>-<topic>.md per repo convention. "
        "Write a LEAN handover: current task state, immediate next steps, critical traps, "
        "and failed approaches — reference DEV-PLAN and file paths on disk rather than "
        "copying design-doc text inline. A successor reading only this handover + DEV-PLAN "
        "+ code on disk must be able to resume under 50% context. "
        "Then stop; do not start new work."
    )


def restore_prompt(repo: str) -> str:
    # RE-GROUND ONLY, then stop (symmetric with cycle_prompt's "do not do work").
    # This turn must NOT carry out the standing task: a restore that also worked
    # crammed a whole unit of work into one turn (natsbus epoch: 19 min / 49 SDK
    # turns / $4), which is only "done" if it beats the wall clock — sandbox's did
    # not (state=timeout). The follow-up work runs as a separate kind='prompt' turn
    # (_after_turn enqueues continue_prompt) under a fresh wall-clock/budget, which
    # also re-arms auto-cycle (it only fires on 'prompt'). ECA-84.
    return (
        "You are a fresh context taking over from your previous epoch. "
        "Restore using the LEAN path: "
        "(1) run `/handover restore` if this repo has the handover skill; otherwise read "
        f"the newest file in {repo}/.claude/handovers/ "
        "(NOT your home directory). "
        "(2) If the handover references a DEV-PLAN, read that file for authoritative task scope. "
        "(3) Treat code on disk as the ground truth for current state. "
        "Do NOT re-read the design-doc corpus or ADR collection wholesale — the handover "
        "already distilled what matters. "
        "Then STOP: reply with a 2-4 sentence summary of your restored state and the immediate "
        "next steps, and END YOUR TURN. Do NOT begin the work itself — a separate follow-up turn "
        "carries it out under a fresh budget."
    )


def continue_prompt() -> str:
    # The work half of a cycle, enqueued after a bounded restore re-grounds (ECA-84).
    # Runs as kind='prompt' so it gets a fresh wall-clock/budget and auto-cycle re-arms.
    return (
        "You have re-grounded from your handover. Continue your standing task now, picking up "
        "at the handover's immediate next steps. Work in bounded increments — your context "
        "auto-cycles when it fills and you can hand off again. If the handover shows the task is "
        "already complete, briefly confirm completion and stop."
    )


def retire_prompt(repo: str) -> str:
    return (
        "You are being retired after an idle period. Write a final session handover NOW: "
        "use `/handover write` if this repo has the handover skill, otherwise write to "
        f"{repo}/.claude/handovers/ (same conventions). "
        "Write a LEAN handover: task state, next steps, critical traps, and failed approaches — "
        "reference DEV-PLAN and file paths on disk rather than copying design-doc text inline. "
        "A successor must be able to resume under 50% context from handover + DEV-PLAN + code alone. "
        "Then stop."
    )


def _discipline_append(
    limits: Limits, cycle_context_pct: int, mcp_server_names: list[str] | None = None
) -> str:
    """Per-turn system-prompt appendix: renders live limits so the agent can self-pace.

    Encodes the three long-op discipline rules from the ECA-60 dogfood campaign
    (epoch-2 restores grounded at 69-79% context; epoch-3 landed 44-45% under
    bounded-turn guidance) plus a command-invocation rule from ECA-6 (decorated
    allowlisted commands were tripping avoidable launcher approval prompts).
    Never hardcode the numeric limits here.
    """
    text = (
        f"TURN DISCIPLINE (enforced by worker-supervisor): "
        f"(1) This turn runs under {limits.wall_clock_s}s wall-clock / "
        f"{limits.max_turns} SDK turns; context auto-cycles at ~{cycle_context_pct}%. "
        f"Keep each turn's scope bounded — split plan and implement across separate turns "
        f"rather than doing a whole large task in one. "
        f"(2) Commit completed work BEFORE starting any long-running operation. "
        f"(3) Run long shell work (docker builds, big installs) backgrounded with "
        f"nohup + a log file; poll with generous-but-bounded timeouts — "
        f"never let one foreground command silently burn the whole wall-clock. "
        f'(4) Run allowlisted commands (e.g. "uv run pytest") PLAINLY — do not chain or '
        f'decorate them with extra shell segments (`; echo "EXIT CODE: $?"`, `&&`, etc). '
        f"Your tool ceiling is matched segment-wise, so an appended segment outside the "
        f"allowlisted prefix trips an avoidable approval prompt even though the underlying "
        f"command is safe."
    )
    if mcp_server_names:
        text += (
            f" (5) MCP SERVERS: this turn was granted {', '.join(mcp_server_names)} in "
            f"addition to your built-in tools. Each is a fresh connection made at turn "
            f"start (a brand-new CLI process backs every turn) and is not guaranteed to "
            f"finish connecting before you start working — ToolSearch or a direct call "
            f"for one of these servers can come back empty/fail on a first attempt purely "
            f"from that startup race, not because the server is actually unavailable. If "
            f"that happens, wait a few seconds and retry once before concluding the server "
            f"is down."
        )
    return text


def session_transcript_path(cwd: str, session_id: str) -> Path:
    """The CLI's cwd-keyed session store: ~/.claude/projects/<sanitized-cwd>/<sid>.jsonl."""
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / sanitized / f"{session_id}.jsonl"


async def _prompt_as_stream(
    prompt: str, startup_grace_s: float = 0.0
) -> AsyncIterator[dict[str, Any]]:
    """can_use_tool requires streaming input (G1) — single-message stream.

    startup_grace_s (ECA-101): a head start for non-'sdk'-type mcp_servers before
    the turn's only prompt is delivered. wait_for_result_and_end_input() (the SDK's
    own pre-first-message gate) only waits on in-process 'sdk' servers — never the
    stdio/http/https servers a worker policy actually grants — so without this,
    slower-to-connect remote/npx servers race the model's first action with no
    guaranteed head start at all.
    """
    if startup_grace_s > 0:
        await asyncio.sleep(startup_grace_s)
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
    mcp_init: list[dict[str, Any]] | None = None
    # ECA-147: did the CLI ever announce the session, and did it emit anything at
    # all? `saw_init` is what distinguishes "the resume was rejected" (the CLI dies
    # with no frame) from "the turn got going and then failed" — measured on CLI
    # 2.1.220: SystemMessage(subtype="init") is the FIRST frame of a fresh turn and
    # of a valid resume alike, and a rejected resume yields nothing at all. The
    # exception TYPE cannot make that distinction; see _is_dead_resume_chain.
    saw_init: bool = False
    frames: int = 0
    # ECA-143: result metadata the CLI reports alongside is_error. None of it has a
    # `turns` column, so it reaches an operator through the failure capsule (and
    # api_error_status through the turn_error/turn_retry events) rather than the row.
    subtype: str | None = None
    api_error_status: int | None = None
    errors: list[str] | None = None
    terminal_reason: str | None = None
    stop_reason: str | None = None

    def diagnostics(self) -> dict[str, Any]:
        """The result-frame metadata a failure capsule carries (ECA-143).

        Motivating failure: a lane turn on mbpm2 died twice with
        `stderr_tail: []` and the single exception string "Claude Code returned
        an error result: success" — self-contradictory and unactionable. The CLI
        had in fact reported the reason (HTTP 429, monthly spend cap) and every
        field naming it was dropped on the floor. `api_error_status` is the one
        that turns that string into a diagnosis; the SDK documents it as set
        exactly when `is_error` is True and `subtype` == "success", which IS this
        failure mode.

        `errors` is the CLI's own error list and so is the only field here that
        can carry model/API text; it is bounded rather than excluded, because it
        is also the most directly diagnostic. That is not a new exposure class —
        a capsule already carries the turn's prompt and raw subprocess stderr,
        which is why ECA-136 writes it 0600 in a 0700 directory.
        """
        errors = self.errors
        if errors:
            errors = [str(e)[:ERROR_ITEM_CHARS] for e in errors[:MAX_ERROR_ITEMS]]
        return {
            "subtype": self.subtype,
            "is_error": self.is_error,
            "api_error_status": self.api_error_status,
            "errors": errors or None,
            "terminal_reason": self.terminal_reason,
            "stop_reason": self.stop_reason,
            "saw_result": self.saw_result,
            "num_turns": self.num_turns,
            # ECA-147: "did the session ever start" is the first question to ask of
            # any failed turn, and until now no capsule field answered it —
            # `saw_result: false` conflates "died before the CLI said hello" with
            # "ran, then died before finishing".
            "saw_init": self.saw_init,
            "frames": self.frames,
        }


def _as_error_list(errors: Any) -> list[Any] | None:
    """Whatever the CLI put in `errors`, as a list — without trusting the type.

    ECA-143 review round, two confirmed defects from a bare `list(msg.errors)`:

    A non-iterable (`errors=5`) raised TypeError inside `_observe`, i.e. inside
    the message loop — so a turn the CLI reported as DONE was converted into a
    supervisor-side error, with a recorded reason pointing at us instead of at
    the protocol violation. A diagnostic must not be able to fail the thing it
    describes.

    A plain string (`errors="monthly spend cap"`) was iterated per CHARACTER and
    then truncated to ten, so the capsule showed ten one-letter "errors" and the
    message was destroyed — worse than dropping it, because it looks like data.

    `message_parser` does no validation on this field, so the type is the CLI's
    word, not a guarantee.
    """
    if errors is None or errors == "" or errors == []:
        return None
    if isinstance(errors, list):
        return errors or None
    if isinstance(errors, (str, bytes)) or not isinstance(errors, Iterable):
        return [errors]  # one value, never a per-character explosion
    try:
        return list(errors) or None
    except Exception:  # noqa: BLE001 — a hostile __iter__ must not fail the turn
        return [repr(errors)[:ERROR_ITEM_CHARS]]


def _status_field(outcome: TurnOutcome) -> dict[str, int]:
    """ECA-143: `api_error_status` as an event field, or nothing.

    `workers events` is the first thing an operator reads, and for this failure
    mode it said only "Claude Code returned an error result: success". The HTTP
    status is what makes that actionable, and the SDK certifies it as metadata
    ("safe to log (no message content)"), so it can go somewhere less protected
    than a capsule. Absent rather than null when the CLI reported none: the event
    log is append-only and read by eye.
    """
    if outcome.api_error_status is None:
        return {}
    return {"api_error_status": outcome.api_error_status}


def _refusal_in_stderr(stderr_tail: Iterable[str]) -> bool:
    """Did the CLI itself say it would not take the session?

    Measured wording on CLI 2.1.220 (both a missing transcript and a present-but-
    rejected one): "No conversation found with session ID: <id>", on stderr, with no
    message frame emitted. Matched loosely because the exact phrasing is the CLI's to
    change — and because this is one of TWO independent signals, not the only one, so
    a wording change degrades to the other rather than to silence.
    """
    return any("no conversation found" in line.lower() for line in stderr_tail)


def _is_dead_resume_chain(
    resume_from: str | None,
    outcome: TurnOutcome,
    exc: BaseException,
    stderr_tail: Iterable[str],
) -> bool:
    """G7's question, asked of the EVIDENCE instead of the exception type (ECA-147).

    G7 says a dead resume chain is never silently continued fresh: end the epoch and
    ground the next one on the handover file. It used to be spelled `except
    ProcessError`, which made the invariant hostage to an SDK implementation detail:

      * A CLI death while `initialize()` is in flight re-raises the transport's RAW
        `ProcessError` — the SDK's read task hands its exception to every pending
        control request (`_internal/query.py`), and `_send_control_request` re-raises
        it. This IS the rejected-resume case, measured on SDK 0.2.128 / CLI 2.1.220:
        `ProcessError(exit_code=1)`, zero frames, stderr "No conversation found with
        session ID: <id>" — for a missing transcript AND for a present-but-rejected
        one.
      * A death AFTER initialize has no pending request, so the same transport error
        is converted to a `{"type": "error"}` frame and `receive_messages` raises a
        BARE `Exception`. That is the ECA-143 shape (an error result, then exit 1).
      * A pre-initialize exit that loses the race with initialize's stdin write
        raises `CLIConnectionError` instead (subprocess_cli.py: "Cannot write to
        process that exited with error"). Read from source, not measured.

    Three types, one meaning. So the first half of the test is what was OBSERVED: the
    CLI announces a live session with `SystemMessage(subtype="init")` — first frame of
    a fresh turn and of a valid resume alike, probed both ways — so a resumed turn with
    no init frame never got as far as a live session.

    The second half is corroboration, and it is NOT optional. "No init frame" alone is
    absence, and plenty of pre-init deaths say nothing about the chain: a missing CLI
    binary or a vanished cwd (`CLINotFoundError` / `CLIConnectionError` out of
    `connect()`, before a read task even exists), a 60s `initialize` control-request
    timeout while MCP servers start, or a crash inside `_prompt_as_stream`'s startup
    grace. Rolling is DESTRUCTIVE — it ends a live epoch, and the restore turn
    re-grounds from the newest handover file, which can be an epoch old — so treating
    those as dead chains would trade a failed turn (recoverable, and the operator sees
    it under keep-on-failure) for a silently discarded session. Review round found
    this; the first version of this predicate had exactly that hole.

    So we require that the CLI ran and refused, evidenced EITHER way:
      * it exited non-zero (`ProcessError`, which only the transport raises, i.e. the
        process started and terminated with a code — the measured refusal shape), or
      * it said so on stderr (`_refusal_in_stderr`).
    Two independent signals for one fact, deliberately: the SDK could stop delivering
    the raw `ProcessError` (that plumbing is an implementation detail and the whole
    reason this function exists), and the CLI could reword its message. Either alone
    still fires. If both ever change at once the capsule now records `saw_init` and
    `frames`, which is the evidence needed to notice.

    Still one thing this does NOT bound: a SYSTEMIC refusal (every resume rejected —
    a read-only `~/.claude/projects`, or `session_transcript_path`'s sanitization
    drifting from what the CLI reads) makes each recovery's own continuation fail the
    same way. `_resume_recovery_would_repeat` is the circuit breaker for that.
    """
    if resume_from is None or outcome.saw_init:
        return False
    return isinstance(exc, ProcessError) or _refusal_in_stderr(stderr_tail)


def _no_retry_reason(outcome: TurnOutcome) -> str | None:
    """Why this failure must not be retried, or None to run the normal ladder (ECA-147).

    The motivating case: a monthly spend cap (HTTP 429) on mbpm2 was retried on every
    turn, doubling the load and writing a `turn_retry` event that implied the failure
    might be transient. `api_error_status` (captured by ECA-143) is the CLI's own
    classification, and the SDK documents it as content-free metadata.

    The reason 4xx is terminal here is NOT that a 429 is inherently unretryable — it
    usually is retryable, and treating it as a spend cap would be reading one incident
    as a rule. It is that **the CLI has already run its own exponential backoff before
    it reports a status at all**: the review round measured ten `system/api_retry`
    frames over ~190s ahead of the `is_error` result. By the time this function sees a
    status, the retryable interpretation has been tried and lost. This ladder rebuilds
    the subprocess immediately, with no backoff of its own, so it can only re-ask a
    question the CLI just spent three minutes on.

    5xx still retries: that is the pre-existing behavior, an overloaded upstream is the
    case where a fresh process plausibly lands differently, and narrowing it further is
    not this task's business. Below 400 is not an error class at all — a status the CLI
    should never pair with `is_error` — so it takes the ordinary ladder rather than
    being silently treated as terminal.
    """
    status = outcome.api_error_status
    if status is None or not 400 <= status < 500:
        return None
    return (
        f"api_error_status={status} is client-side (quota/auth/request), and the CLI "
        "already exhausted its own backoff before reporting it — an immediate, "
        "backoff-free second attempt cannot change it"
    )


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
        self._sweep_orphan_mcp_configs()
        for w in await self._reg.list_workers():
            self._ensure_runner(w["name"])

    def _sweep_orphan_mcp_configs(self) -> None:
        """ECA-135: drop credential files a SIGKILLed daemon left behind.

        The turn-end purge covers every in-process path, but a killed process runs no
        `finally`, and an orphan for a lane that is never prompted again would sit there
        indefinitely — the permanent exposure this fix exists to avoid. No turn can be
        in flight at boot (one daemon per socket), so everything here is an orphan.
        Never raises: a boot that dies here would take the whole daemon down.

        Honest limit on that premise: the socket preflight this now runs behind keys on
        the SOCKET, and SUPERVISOR_SOCKET exists so a deep SUPERVISOR_HOME can use a
        short one — so two daemons sharing a HOME with different socket overrides both
        pass it, and the second would sweep the first's in-flight files. That needs a
        deliberate misconfiguration; the honest fix is an flock on `home`, not on the
        socket. Recorded rather than silently assumed away.
        """
        try:
            d = self._cfg.mcp_config_dir
            if d.is_symlink():
                raise OSError(f"{d} is a symlink; refusing to sweep through it")
            for path in d.glob("*.json"):
                path.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001 — boot must survive a cleanup failure
            self._events.emit(
                DAEMON_KEY, "mcp_config_sweep_failed", error=f"{type(e).__name__}: {e}"
            )

    async def stop(self) -> None:
        """Cancel every runner/watchdog and wait for them — BOUNDED (ECA-148).

        The unbounded `await task` this replaces could not return at all while a
        turn was in flight, because `_worker_loop` swallowed the runner's own
        cancellation (fixed there; this is the backstop, not the cure). A bound
        belongs here regardless: the SDK's `Query.close()` is documented as
        SHIELDED and explicitly *not* bounded, so a wedged transport close cannot
        be interrupted by the cancellation this sends — `stop()` would wait on it
        forever, and the daemon's SIGTERM path awaits `stop()` before it closes
        the registry and exits.

        Anything still pending at the deadline is ABANDONED, loudly: the process
        is on its way out either way, and an event naming the tasks beats a
        shutdown that never completes. Under pm2 the practical ceiling is much
        lower than this grace anyway (`kill_timeout` defaults to 1600ms, after
        which the tree is SIGKILLed) — the grace exists for a daemon run by hand
        and for tests, not to hold pm2 open.
        """
        tasks = [*self._runners.values(), *self._watchdogs]
        pending: set[asyncio.Task[None]] = set()
        if tasks:
            for task in tasks:
                task.cancel()
            _, pending = await asyncio.wait(tasks, timeout=STOP_GRACE_S)
            for task in tasks:
                # Retrieve outcomes so a runner that died of its own exception
                # does not surface as "Task exception was never retrieved" during
                # interpreter shutdown. The old per-task `await` did this
                # implicitly; `asyncio.wait` does not.
                if task.done():
                    with suppress(asyncio.CancelledError, Exception):
                        task.exception()
        if pending:
            self._events.emit(
                DAEMON_KEY, "stop_incomplete", grace_s=STOP_GRACE_S,
                abandoned=sorted(t.get_name() for t in pending),
            )
        self._runners.clear()
        self._watchdogs.clear()

    async def spawn(
        self, name: str, repo: str, policy: WorkerPolicy | None = None
    ) -> dict[str, Any]:
        await self._validate_worker_name(name)
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
        turn_id = await self._reg.enqueue_turn(
            worker["name"], cycle_prompt(worker["repo"]), kind="cycle_handover"
        )
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

    async def remove(self, name: str) -> None:
        """Purge a terminal (killed/retired) worker and its history, freeing the
        PRIMARY-KEY name for a fresh spawn (ECA-99: `kill` retains the row, so a
        same-name respawn hits the duplicate guard). Refuses to purge a live worker
        — kill it first; its loop must have exited before the row is deleted."""
        worker = await self._reg.get_worker(name)
        if worker is None:
            raise ValueError(f"no such worker: {name}")
        if worker["status"] not in WORKER_GONE:
            raise ValueError(
                f"worker {name!r} is {worker['status']}; kill it before remove"
            )
        # ECA-135: belt-and-braces. _worker_loop purges at every turn end, so this only
        # catches a file left by a daemon that died mid-turn. Never raises, so a
        # cleanup failure cannot make a worker permanently unremovable.
        self._purge_mcp_config(name)
        await self._reg.delete_worker(name)
        for bookkeeping in (self._runners, self._kicks, self._current):
            bookkeeping.pop(name, None)
        self._events.emit(name, "worker_removed")

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
            await self._reg.enqueue_turn(w["name"], retire_prompt(w["repo"]), kind="retire_handover")
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
                    # ECA-148: WHOSE cancellation is this? Two callers cancel into
                    # this await and they want opposite things. `kill()` cancels the
                    # TURN task and deliberately leaves the runner alive so the loop
                    # can re-read worker status; `stop()` cancels the RUNNER and
                    # needs it to exit. Both arrive here as a CancelledError with
                    # `task.cancelled()` true — asyncio cancels the awaited future
                    # when the awaiting task is cancelled — so the inner task's own
                    # state cannot tell them apart. It answered "killed" for both,
                    # and `continue` then swallowed the runner's cancellation: the
                    # loop went round, parked in the 15s idle wait below, and
                    # `stop()`'s await never returned. Ask whether WE were cancelled
                    # instead; that is true only on the `stop()` path.
                    if _self_cancelled():
                        # Leave the turn row wherever it is on purpose: if it is
                        # still claimed/running, `boot_reconcile` resets it to
                        # queued (redeliveries + 1) and the interrupted work is
                        # redelivered on the next boot — the same at-least-once
                        # contract a crash already has.
                        #
                        # RE-READ the row before saying so. Review round: this
                        # emitted unconditionally, and the widest window for a
                        # cancel to land here is the tail of `_run_turn` AFTER
                        # `finish_turn` has committed — so the common case was an
                        # event claiming a redelivery for a turn that was already
                        # `done` and would never be requeued. An event that lies
                        # about durability is worse than no event.
                        with suppress(asyncio.CancelledError, Exception):
                            row = await self._reg.get_turn(turn["id"])
                            if row is not None and row["state"] not in TURN_TERMINAL:
                                self._events.emit(
                                    name, "turn_interrupted", turn_id=turn["id"],
                                    state=row["state"], reason="daemon stop",
                                )
                        raise
                    if task.cancelled():
                        continue  # the turn was killed; loop decides via status
                    task.cancel()
                    raise
                finally:
                    self._current.pop(name, None)
                    # ECA-135: the credential file lives no longer than the turn that
                    # needs it, on every in-process path — normal completion, every
                    # failure-ladder return, kill-cancellation and daemon stop all land
                    # in this finally. (A SIGKILLed daemon still leaves one; that is
                    # what the boot sweep in start() is for.) Argv's one virtue was
                    # being transient, and a file that outlived `kill` would have traded
                    # a turn-scoped exposure for a permanent one.
                    self._purge_mcp_config(name)
            # ECA-148 review round: let this FINISH even when the daemon is stopping.
            # `_after_turn` chains lifecycle state across several awaits — a
            # `cycle_handover` rolls the epoch and THEN enqueues the restore turn —
            # and `boot_reconcile` cannot heal a tear between them: it requeues
            # claimed/running TURNS, and the lost restore was never a turn row at all.
            # A lane cancelled in that gap comes back on a fresh epoch with an empty
            # queue and no handover restore, i.e. silently amnesiac. It is a handful of
            # DB writes, so it is worth waiting out; `stop()`'s own grace is the outer
            # bound, and this inner one keeps a wedged registry from eating it.
            after = asyncio.ensure_future(self._after_turn(name, turn["id"]))
            try:
                await asyncio.shield(after)
            except asyncio.CancelledError:
                if not _self_cancelled():
                    after.cancel()
                    raise
                # `shield` only detached US from it; `after` is still running.
                # `asyncio.wait` never cancels what it waits on.
                await asyncio.wait({after}, timeout=AFTER_TURN_GRACE_S)
                if not after.done():
                    after.cancel()
                    self._events.emit(
                        name, "after_turn_abandoned", turn_id=turn["id"],
                        grace_s=AFTER_TURN_GRACE_S,
                    )
                else:
                    with suppress(asyncio.CancelledError, Exception):
                        after.exception()
                raise

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

        # Budget gate, pre-spawn (AC-WS-5): a breached epoch refuses new turns —
        # EXCEPT lifecycle turns (ECA-99), which must run so a capped lane can
        # cycle/retire out instead of wedging (see LIFECYCLE_KINDS).
        is_lifecycle = turn["kind"] in LIFECYCLE_KINDS
        budget_floor = LIFECYCLE_BUDGET_RESERVE_USD if is_lifecycle else 0.01
        remaining_budget = limits.max_budget_usd_per_epoch - (epoch["cost_usd"] or 0.0)
        if remaining_budget <= 0 and not is_lifecycle:
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
            "max_budget_usd": max(budget_floor, round(remaining_budget, 4)),
            "model": policy.model,
            "allow_env": policy.allow_env,
            "mcp_servers": sorted(policy.mcp_servers.keys()),
            "wall_clock_s": limits.wall_clock_s,
        }

        # ECA-135: written ONCE per turn, before the retry ladder — a retry reuses
        # the same file rather than rewriting the lane's credentials on each attempt.
        # This is the first filesystem touch _run_turn makes before query(), so it is
        # also the first thing here that can raise OUTSIDE the attempt loop's handlers.
        # Nothing supervises a runner task, so an escape would kill the lane's loop and
        # leave the turn `claimed` and the worker `running` forever, with no event and
        # no capsule. Fail the TURN through the normal ladder instead.
        try:
            mcp_arg = self._write_mcp_config(name, turn_id, policy.mcp_servers)
        except (OSError, ValueError) as e:
            await self._fail_turn(
                name, turn_id, TurnOutcome(), f"mcp config write failed: {e}",
                options_snapshot, deque(), resume_from, policy,
            )
            return

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
            # ECA-145: [total dispatches, AskUserQuestion-classified dispatches] —
            # compared against outcome.tools at turn end by
            # Engine._check_policy_hook_gap. Reset every attempt, same as
            # `outcome` below: a retried attempt gets its own fresh count.
            hook_calls = [0, 0]
            options = ClaudeAgentOptions(
                cwd=worker["repo"],
                resume=resume_from,
                setting_sources=["project"],
                tools=policy.base_tools(),
                # NOTHING is pre-approved here. That is necessary but NOT sufficient
                # for "every privileged call routes through the gate", which is what
                # this comment used to claim and which was false (ECA-142): the CLI
                # auto-approves read-only Bash inside the cwd on its own, and those
                # calls never reach can_use_tool. The total enforcement point is the
                # PreToolUse policy hook registered below; see gate.make_policy_hook.
                # Pre-approving AskUserQuestion would additionally bypass can_use_tool
                # and the tool errors headless.
                allowed_tools=[],
                max_turns=limits.max_turns,
                max_budget_usd=max(budget_floor, round(remaining_budget, 4)),
                model=policy.model,
                # AC#2 (ECA-72): retain the default Claude Code system prompt and
                # append live per-turn limits so the agent can self-pace without
                # relying on the orchestrator to encode them in every task prompt.
                system_prompt={
                    "type": "preset",
                    "preset": "claude_code",
                    "append": _discipline_append(
                        limits,
                        self._cfg.cycle_context_pct,
                        sorted(policy.mcp_servers.keys()),
                    ),
                },
                env=build_worker_env(
                    self._boot_env,
                    policy.allow_env,
                    mcp_tool_timeout_ms=(self._cfg.question_timeout_s + 300) * 1000,
                ),
                # Per-lane MCP grant (ECA-100): the supervisor hands the worker
                # EXACTLY the servers in its policy — strict mode when any are
                # granted so an ambient repo .mcp.json can't widen the surface;
                # off (default discovery, which finds nothing at the workspace
                # root) when the lane has no MCP grant, preserving prior behavior.
                # ECA-135: a 0600 FILE PATH, never the credential-bearing dict —
                # see _write_mcp_config for what that does and does not buy.
                mcp_servers=mcp_arg,
                strict_mcp_config=bool(policy.mcp_servers),
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
                        ),
                        # ECA-142: the TOTAL policy point. `matcher=None` fires for
                        # every tool call, including the ones the CLI auto-approves
                        # and therefore never route through can_use_tool.
                        #
                        # Its own matcher rather than folded into the one above, and
                        # the reason is NOT the one first written here. Review checked
                        # the CLI: when every matcher on an event is an SDK callback —
                        # this daemon's shape — 2.1.220 awaits them SEQUENTIALLY in
                        # registration order, not concurrently (concurrency applies
                        # only in its general branch). Separate matchers are still
                        # right, for a stronger reason: `getMatchingHooks` filters the
                        # "AskUserQuestion" entry out for every other tool name, so
                        # the bridge's park cannot gate any other tool. FOLDING them
                        # into one matcher is what would have gated everything.
                        #
                        # No `timeout=` here: in that all-callback path the CLI never
                        # reads one (which also makes the sibling's timeout above a
                        # no-op today). It starts to matter only if a non-callback
                        # PreToolUse hook ever joins this event.
                        HookMatcher(
                            matcher=None,
                            hooks=[
                                make_policy_hook(
                                    worker=name,
                                    repo_root=Path(worker["repo"]),
                                    policy=policy,
                                    events=self._events,
                                    turn_id=turn_id,
                                    hook_calls=hook_calls,
                                )
                            ],
                        ),
                    ]
                },
                stderr=stderr_tail.append,
            )
            outcome = TurnOutcome()
            try:
                async with asyncio.timeout(limits.wall_clock_s):
                    grace = self._cfg.mcp_startup_grace_s if policy.mcp_servers else 0.0
                    async for msg in query(
                        prompt=_prompt_as_stream(turn["prompt"], grace), options=options
                    ):
                        self._observe(name, turn_id, msg, outcome)
                break  # stream completed
            except (asyncio.TimeoutError, TimeoutError):
                # Wall-clock breach: cancellation closed the transport, which
                # escalates SIGTERM->SIGKILL on the subprocess group.
                # ECA-143 review round: this path dropped SIX fields, not three, and
                # the missing three were the expensive ones. `finish_turn` writes every
                # column unconditionally (no COALESCE), and the epoch's spend is
                # incremented only from the `cost_usd` it is handed — so a turn that
                # burned money and then hit the wall clock was charged NOTHING against
                # max_budget_usd_per_epoch, and `status`'s context_pct (read off the
                # last finished turn's usage) came back null. Wall-clock breaches are
                # exactly the expensive turns.
                await self._reg.finish_turn(
                    turn_id, "timeout",
                    session_id=outcome.session_id,
                    result_text=outcome.result_text,
                    is_error=outcome.is_error,
                    cost_usd=outcome.cost_usd,
                    duration_ms=outcome.duration_ms,
                    num_turns=outcome.num_turns,
                    usage=outcome.usage,
                    error=f"wall clock exceeded ({limits.wall_clock_s}s)",
                    tools=outcome.tools,
                )
                self._events.emit(
                    name, "turn_timeout", turn_id=turn_id, **_status_field(outcome)
                )
                await self._finish_failure_capsule(
                    name, turn_id, "timeout", options_snapshot, list(stderr_tail),
                    [resume_from], outcome=outcome,
                )
                self._emit_mcp_diagnostics(name, turn_id, policy, outcome.mcp_init, stderr_tail)
                await self._reg.set_worker_status(name, "idle", active=True)
                return
            except asyncio.CancelledError:
                raise  # kill() owns the record
            except Exception as e:  # noqa: BLE001 — G2: any death, whatever its type
                # ECA-147: ONE handler for every failure the SDK can deliver. There
                # used to be a `except ProcessError` clause ahead of this one holding
                # all of G7, and which of the two ran depended on WHERE inside the SDK
                # the CLI's exit was noticed (pending control request -> the raw
                # ProcessError; after initialize -> a bare Exception; lost write race
                # -> CLIConnectionError). Same event, three types: a decision that
                # matters cannot be spelled as an exception type.
                exit_code = e.exit_code if isinstance(e, ProcessError) else None
                reason = f"{type(e).__name__}: {e}"
                if exit_code is not None:
                    reason += f" (exit={exit_code})"
                dead_chain = _is_dead_resume_chain(resume_from, outcome, e, stderr_tail)
                if dead_chain and await self._resume_recovery_would_repeat(name):
                    # The breaker tripped: G7 already recovered this epoch and the
                    # recovery's own work is failing the same way. Fail the turn instead
                    # (keep-on-failure) so it stops loudly rather than burning restore
                    # turns. Distinct event: this is not an ordinary error, it is G7
                    # declining to try the same cure twice.
                    self._events.emit(
                        name, "resume_recovery_exhausted", turn_id=turn_id,
                        resume=resume_from, **_status_field(outcome),
                    )
                    await self._fail_turn(
                        name, turn_id, outcome,
                        f"resume failed again inside a resume_failed recovery"
                        f" (not rolling the epoch a second time): {reason}",
                        options_snapshot, stderr_tail, resume_from, policy,
                        no_retry="the previous epoch already ended in resume_failed and"
                        " nothing has resumed successfully since — an operator has to"
                        " look at the session store",
                    )
                    return
                if dead_chain:
                    # G7: the chain is dead. Never silently fresh — end the epoch,
                    # open the next one grounded on the handover file.
                    # ECA-143 review round: same six as the timeout path. Persisting
                    # session_id here cannot resurrect the dead chain — this branch
                    # rolls the epoch, and _pick_resume_target only ever considers
                    # session ids WHERE epoch_id = <the new epoch>.
                    # Those fields are now DEFENSIVE rather than load-bearing: this
                    # branch requires that no init frame was seen, and a ResultMessage
                    # only ever follows one, so on CLI 2.1.220 there is nothing here to
                    # persist. Kept unconditional anyway — `finish_turn` has no COALESCE,
                    # so an omitted keyword writes NULL, and the day a CLI reports a
                    # result without announcing the session this must not be the one
                    # path that loses a turn's spend.
                    await self._reg.finish_turn(
                        turn_id, "error",
                        session_id=outcome.session_id,
                        result_text=outcome.result_text,
                        is_error=outcome.is_error,
                        cost_usd=outcome.cost_usd,
                        duration_ms=outcome.duration_ms,
                        num_turns=outcome.num_turns,
                        usage=outcome.usage,
                        tools=outcome.tools,
                        error=f"resume failed: {reason}",
                    )
                    self._events.emit(
                        name, "resume_failed", turn_id=turn_id, resume=resume_from,
                        **_status_field(outcome),
                    )
                    await self._finish_failure_capsule(
                        name, turn_id, "resume_failed", options_snapshot,
                        list(stderr_tail), [resume_from],
                        outcome=outcome,
                    )
                    self._emit_mcp_diagnostics(name, turn_id, policy, outcome.mcp_init, stderr_tail)
                    await self._reg.roll_epoch(name, "resume_failed")
                    await self._reg.enqueue_turn(
                        name, restore_prompt(worker["repo"]), kind="restore"
                    )
                    await self._reg.set_worker_status(name, "idle", active=True)
                    self._kick(name)
                    return
                no_retry = _no_retry_reason(outcome)
                if attempt == 1 and no_retry is None:
                    self._events.emit(
                        name, "turn_retry", turn_id=turn_id, error=str(e),
                        **_status_field(outcome),
                    )
                    continue
                await self._fail_turn(
                    name, turn_id, outcome, reason, options_snapshot,
                    stderr_tail, resume_from, policy,
                    # Only when the retry was actually SKIPPED. Review round: computed
                    # from attempt 2's outcome this claimed a suppression that had not
                    # happened — a turn that retried on a statusless failure and then
                    # came back 429 carried both a turn_retry row and a "not retried"
                    # reason, which is a lie in exactly the case the field exists for.
                    no_retry=no_retry if attempt == 1 else None,
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
                is_error=outcome.is_error,  # ECA-143: the only field this path dropped
                cost_usd=outcome.cost_usd,
                duration_ms=outcome.duration_ms,
                num_turns=outcome.num_turns,
                usage=outcome.usage,
                tools=outcome.tools,
                error="question timed out unanswered",
            )
            self._events.emit(
                name, "turn_question_timeout", turn_id=turn_id, **_status_field(outcome)
            )
            await self._finish_failure_capsule(
                name, turn_id, "question_timeout", options_snapshot,
                list(stderr_tail), [resume_from, outcome.session_id],
                outcome=outcome,
            )
            self._emit_mcp_diagnostics(name, turn_id, policy, outcome.mcp_init, stderr_tail)
            await self._reg.set_worker_status(name, "idle", active=True)
            return

        if not outcome.saw_result:
            await self._fail_turn(
                name, turn_id, outcome, "stream ended without a ResultMessage",
                options_snapshot, stderr_tail, resume_from, policy,
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
            **_status_field(outcome),  # ECA-143: state="error" without an exception
        )
        self._emit_mcp_diagnostics(name, turn_id, policy, outcome.mcp_init, stderr_tail)
        # ECA-145 review round: checked ONLY here, not on every terminal branch
        # `_emit_mcp_diagnostics` also covers. A turn that timed out, died mid-
        # stream, or hit a question timeout may have STREAMED an assistant
        # message with several queued tool uses before the interruption reached
        # it — `_observe` records every `ToolUseBlock` the instant the message
        # arrives, but the CLI dispatches this hook to each tool right before
        # THAT tool executes, one at a time. A turn cut short between "message
        # observed" and "every queued tool actually ran" would show a "gap" that
        # is really just "didn't get there yet", not a dispatch regression. Only
        # a stream that reached its own natural end (this branch — `done` or a
        # reported `error`, either way the CLI finished processing everything it
        # queued) can tell the two apart. Detection is deferred, not lost: the
        # threat model (ECA-145) is a persistent SDK/CLI regression, which shows
        # up on the many turns that DO complete normally, not a one-off.
        self._check_policy_hook_gap(name, turn_id, outcome, (hook_calls[0], hook_calls[1]))
        if outcome.session_id:
            watchdog = asyncio.create_task(
                self._verify_transcript_persisted(
                    name, worker["repo"], outcome.session_id, turn_id
                ),
                # ECA-148 review round: named, because `stop_incomplete.abandoned`
                # exists to say WHAT was abandoned and an unnamed task reports as
                # "Task-23" — useless in the one event whose whole job is naming.
                name=f"transcript-watchdog-{name}-turn{turn_id}",
            )
            self._watchdogs.add(watchdog)
            watchdog.add_done_callback(self._watchdogs.discard)
        if state == "error":
            await self._finish_failure_capsule(
                name, turn_id, "result_error", options_snapshot,
                list(stderr_tail), [resume_from, outcome.session_id],
                outcome=outcome,
            )
        await self._reg.set_worker_status(name, "idle", active=True)

    def _transcript_exists(self, cwd: str, session_id: str) -> bool:
        return session_transcript_path(cwd, session_id).exists()

    async def _pick_resume_target(
        self, name: str, cwd: str, epoch_id: int, turn_id: int
    ) -> str | None:
        """Newest session id in the epoch whose transcript is actually on disk.

        Observed on CLI 2.1.165 (SDK mode): the transcript is written at process
        exit, and the SDK's close() (stdin-close -> 5s -> SIGTERM -> SIGKILL)
        races it, so a turn can report a session id that never persists.
        Resuming that id fails with 'No conversation found'. Skipping to the
        newest persisted id loses one turn of context instead of the whole epoch;
        G7 remains the backstop. NOT re-measured on the current 2.1.220 bundle
        (ECA-138) — the guard is kept because it costs one turn when the race is
        gone and saves an epoch when it is not.
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

    async def _resume_recovery_would_repeat(self, name: str) -> bool:
        """Is this a second dead chain inside a recovery that has not worked? (ECA-147)

        G7's cure for a dead chain is: roll the epoch, restore from the handover file,
        and (ECA-84) auto-enqueue one continuation. That continuation resumes the RESTORE
        turn's own session — so if resuming is systemically broken (a read-only
        `~/.claude/projects`, a `session_transcript_path` that no longer matches what the
        CLI reads), it is refused too, and G7 rolls again. Each roll costs a real restore
        turn, and each new epoch gets a fresh `max_budget_usd_per_epoch`, so the epoch
        budget cannot bound it. Review round measured the unmutated loop: 40 rolls, 82
        CLI invocations, no natural end.

        The breaker: if this epoch was opened BY a resume_failed roll and nothing in it
        has resumed a session successfully, the cure has already been tried and failed.
        A `done` restore turn does not count as success — it runs with no `resume_from`
        at all, so it proves the CLI works, not that this lane can resume.

        Deliberately not a counter on the worker: the condition resets itself the moment
        a resumed turn finishes cleanly, so a genuine dead chain months later still gets
        its recovery.
        """
        if await self._reg.previous_epoch_end_reason(name) != "resume_failed":
            return False
        epoch = await self._reg.current_epoch(name)
        if epoch is None:
            return False
        return not await self._reg.has_successful_resume(epoch["id"])

    async def _fail_turn(
        self,
        name: str,
        turn_id: int,
        outcome: TurnOutcome,
        error: str,
        options_snapshot: dict[str, Any],
        stderr_tail: deque[str],
        resume_from: str | None,
        policy: WorkerPolicy,
        no_retry: str | None = None,
    ) -> None:
        """Terminal error after the retry: record, capsule, keep the epoch
        (keep-on-failure, Amendment A6) — the orchestrator decides what's next.

        `no_retry` (ECA-147) is set when the ladder deliberately skipped the retry,
        and names why. It goes on the event because that is where an operator looks
        first, and a MISSING retry is otherwise invisible — the absence of a
        `turn_retry` row reads identically to a ladder that never ran.

        ECA-143: this used to persist cost/duration/usage/tools and DROP
        result_text, is_error and num_turns — the three fields that say what the
        CLI reported. The tell in the live capsule was a `duration_ms` and a
        `usage` blob (so a ResultMessage plainly HAD been observed) sitting beside
        `result_text: null`. A failure path must persist at least as much as the
        success path, not less.

        No `Engine._check_policy_hook_gap` call here (ECA-145 review round): every
        caller of this method is, by definition, an interrupted or abnormal
        termination, which is exactly where that check cannot tell "a queued tool
        never got its turn to be dispatched" apart from a real dispatch regression
        — see the comment at this engine's one call site for the full reasoning.
        """
        await self._reg.finish_turn(
            turn_id, "error",
            session_id=outcome.session_id,
            result_text=outcome.result_text,
            is_error=outcome.is_error,
            cost_usd=outcome.cost_usd,
            duration_ms=outcome.duration_ms,
            num_turns=outcome.num_turns,
            usage=outcome.usage,
            tools=outcome.tools,
            error=error,
        )
        self._events.emit(
            name, "turn_error", turn_id=turn_id, error=error,
            **({"no_retry": no_retry} if no_retry else {}),
            **_status_field(outcome),
        )
        await self._finish_failure_capsule(
            name, turn_id, "error", options_snapshot, list(stderr_tail),
            [resume_from, outcome.session_id], outcome=outcome,
        )
        self._emit_mcp_diagnostics(name, turn_id, policy, outcome.mcp_init, stderr_tail)
        await self._reg.set_worker_status(name, "idle", active=True)

    def _emit_mcp_diagnostics(
        self,
        name: str,
        turn_id: int,
        policy: WorkerPolicy,
        mcp_init: list[dict[str, Any]] | None,
        stderr_tail: deque[str] | list[str],
    ) -> None:
        """ECA-101 AC1: surface a granted-MCP lane's connect evidence on EVERY
        terminal state, not just a successful one — a failed turn already gets a
        capsule (Amendment A6), but that capsule carries no MCP-specific context,
        and a genuinely unreachable/never-connecting server is exactly the kind of
        failure an operator debugging "is this MCP server actually up" most wants
        surfaced alongside the turn's outcome."""
        if not policy.mcp_servers:
            return
        self._events.emit(
            name, "turn_mcp_diagnostics", turn_id=turn_id,
            granted=sorted(policy.mcp_servers.keys()),
            mcp_init=mcp_init,
            stderr_tail=list(stderr_tail),
        )

    def _check_policy_hook_gap(
        self, name: str, turn_id: int, outcome: TurnOutcome, hook_calls: tuple[int, int]
    ) -> None:
        """ECA-145: detect the PreToolUse policy hook silently ceasing to fire.

        ECA-142 made this hook the TOTAL enforcement point for the subset of calls
        the CLI auto-approves on its own (read-only Bash inside the cwd never
        reaches `can_use_tool` at all). The hook fails closed for exceptions
        raised INSIDE it, but nothing detected the hook simply not being
        DISPATCHED — a future SDK/CLI change, or `matcher=None` losing its
        documented universality — which would silently restore the exact ECA-142
        defect. The unit suite cannot see this either: it calls the hook function
        directly, which is the call the product was failing to make (see
        test_live_gate.py's own rationale, and ECA-137/139/142 before it).

        `hook_calls` is `(total dispatches, dispatches classified as
        AskUserQuestion)`. Every observed `ToolUseBlock` should have a matching
        dispatch — the hook is registered with `matcher=None`, which fires for
        every tool call, including AskUserQuestion (which then no-ops inside it —
        see `make_policy_hook`). AskUserQuestion tool uses are excluded from
        `required` below because that tool has its own dedicated escalation
        bridge, but a naive `total >= required_excluding_askq` comparison would be
        WRONG (review round finding): each AskUserQuestion dispatch would silently
        forgive one un-dispatched call to a completely different tool, since both
        just increment the same total. Subtracting the classified count first —
        `total - askq_calls` — compares dispatches for the tools this check
        actually cares about against `required`, one for one.

        AC#3's decision, made explicitly here because this is the one place that
        acts on it: WARN, do not fail the turn. The unpoliced call has already
        executed by the time this runs, so failing the turn cannot undo it and
        buys no additional security boundary. A real gap is a property of the
        SDK/CLI pin, not of one turn — it would recur on every subsequent tool
        call, in every concurrent lane, fleet-wide. Failing every turn would flood
        failure capsules and burn epoch budget across the whole fleet for a
        condition an operator fixes by re-pinning a working SDK/CLI version, not
        by retrying turns. A distinct event name — not `tool_denied`, which means
        something already fired and stopped a call — is what actually gets this
        looked at; the same reasoning ECA-141's `guard:skipped` record already
        applied to the narrower malformed-`guard_hooks` case.

        Disclosed residual (review round): the CLI rejects a schema-invalid
        `tool_input` (a Zod `safeParse`/`validateInput` failure) before this hook
        is ever dispatched for that call, and the `ToolUseBlock` is still counted
        in `outcome.tools` — a genuinely malformed tool call from the model, on an
        otherwise normal turn, can therefore read as a gap with nothing actually
        wrong. Distinguishing "never dispatched because rejected pre-hook" from
        "never dispatched because the SDK/CLI stopped calling this hook" is not
        possible from this daemon's side without depending on undocumented CLI
        internals — exactly what this codebase otherwise refuses to do (see
        `_split_command_segments`'s own docstring). WARN-only makes this a cheap
        false alarm rather than a wrongly-failed turn: an operator who checks a
        `policy_hook_gap` against a `done` turn with no other symptom can dismiss
        it as this residual rather than a real regression.
        """
        total, askq_calls = hook_calls
        required = sum(1 for t in outcome.tools if t != "AskUserQuestion")
        non_askq_calls = total - askq_calls
        if non_askq_calls < required:
            self._events.emit(
                name, "policy_hook_gap", turn_id=turn_id,
                tool_uses=required, hook_invocations=non_askq_calls,
            )

    async def _validate_worker_name(self, name: str) -> None:
        """ECA-135: a worker name becomes a FILENAME, so it has to be one.

        Names were never validated anywhere — not the CLI, not the control surface,
        not the registry. That was survivable while every name-derived path was
        create-or-append (`logs/<name>.jsonl`, capsules). It stopped being survivable
        when this change started TRUNCATING and UNLINKING a name-derived path: review
        demonstrated `spawn ../../.claude` overwriting the operator's real
        `~/.claude.json` — which is where the deploy generator reads live credentials
        from — and `remove` then deleting it. The caller is an orchestrator LLM driving
        `workers spawn` over Bash, so the name is not reliably operator-authored text.

        Case-folding is checked too, and for the same reason: APFS is case-insensitive
        while the registry's TEXT PRIMARY KEY is not, so `Ultra1` and `ultra1` are two
        distinct workers sharing one credential file — last writer wins, and a lane can
        start with another lane's bearers. That is precisely the cross-lane leak this
        task is about, so it must not be reintroduced by the fix for it.
        """
        require_safe_worker_name(name)
        # NB: this read-then-insert is a TOCTOU — two concurrent spawns of `Ultra1` and
        # `ultra1` could both pass. The control surface handles connections in separate
        # tasks, so it is possible, just unlikely (spawns arrive serially in practice).
        # A real fix needs a case-insensitive uniqueness constraint in the registry.
        for existing in await self._reg.list_workers(include_gone=True):
            if existing["name"] != name and existing["name"].casefold() == name.casefold():
                raise ValueError(
                    f"worker name {name!r} collides case-insensitively with "
                    f"{existing['name']!r}; they would share one MCP config file"
                )

    def _mcp_config_paths(self, worker: str) -> list[Path]:
        """Every config file belonging to this worker.

        A glob rather than a derived name because the filename carries a random
        component (see _write_mcp_config). Validates the name HERE, at the point of
        path derivation, so every consumer — write, turn-end purge, remove — is covered
        by one guard rather than relying on spawn-time validation that a row persisted
        before it existed never passed through.
        """
        require_safe_worker_name(worker)  # no glob metacharacters survive this charset
        # '-' is the field separator AND a legal name character, so a bare
        # `<worker>-*.json` glob matches a DIFFERENT lane whose name extends this one:
        # purging `ultra` would delete `ultra-2`'s live, in-flight config, and with
        # strict_mcp_config that lane then runs with zero servers and no error. Match
        # the full shape instead of trusting the prefix.
        mine = re.compile(rf"{re.escape(worker)}-\d+-[0-9a-f]{{16}}\.json")
        d = self._cfg.mcp_config_dir
        if d.exists() and not d.is_dir():
            # Path.glob swallows ENOTDIR and returns nothing, so without this the purge
            # would report success on a broken config dir and an un-granted lane would
            # give no signal at all. (A MISSING dir is normal and yields [] correctly.)
            raise OSError(f"{d} exists but is not a directory")
        if d.is_symlink():
            # The WRITE refuses a symlinked dir; if the purge did not, the guard would
            # be worse than useless — refusing to create a file there while happily
            # DELETING whatever is already there. A confused-deputy unlink is strictly
            # worse than the truncate the write guard exists to prevent.
            raise OSError(f"{d} is a symlink; refusing to purge MCP configs through it")
        return sorted(p for p in d.glob(f"{worker}-*.json") if mine.fullmatch(p.name))

    def _purge_mcp_config(self, worker: str) -> None:
        """Remove this lane's config file(s). NEVER raises.

        Called from a `finally` and from `remove`, so an escape here would kill the
        worker's runner loop and skip `_after_turn` — no epoch roll, no restore
        enqueue, no auto-cycle, no retirement — while the lane still answered prompts.
        That is exactly the failure this task already fixed once at the write site;
        `unlink(missing_ok=True)` swallows only FileNotFoundError, so ENOTDIR/EACCES/
        EPERM (and an invalid persisted name) still escape unless caught here.
        """
        def _tell(key: str, event: str, **fields: Any) -> None:
            # emit() is a file open, so an emit from inside a handler can escape and
            # re-create the very wedge this method exists to prevent. Still required
            # after ECA-137: that guard covers a bad KEY (which emit now re-keys rather
            # than raising on), and nothing else — ENOSPC, EACCES and a symlinked logs/
            # all still reach here.
            try:
                self._events.emit(key, event, **fields)
            except Exception:  # noqa: BLE001 — reporting a cleanup failure cannot fail loudly
                pass

        try:
            paths = self._mcp_config_paths(worker)
        except Exception as e:  # noqa: BLE001 — invalid persisted name, or a symlinked dir
            # Reported under the DAEMON key, never the worker's: EventLog derives its
            # filename from that key, so emitting under a traversing name would trade a
            # traversal-unlink for a traversal-write. Caught by this method's own test.
            # ECA-137 made EventLog refuse such a key itself and re-key the record here
            # anyway, so this is now belt-and-braces — kept because passing the key
            # explicitly yields a clean record instead of one stamped `log_key_refused`.
            _tell(
                DAEMON_KEY, "mcp_config_purge_refused",
                lane=worker, error=f"{type(e).__name__}: {e}",
            )
            return
        try:
            for path in paths:
                path.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001 — a cleanup failure must never wedge a lane
            # `worker` is known-valid here (it survived _mcp_config_paths).
            _tell(worker, "mcp_config_purge_failed", error=f"{type(e).__name__}: {e}")

    def _write_mcp_config(
        self, worker: str, turn_id: int, servers: dict[str, Any]
    ) -> str | dict[str, Any]:
        """ECA-135: hand the CLI a 0600 config FILE PATH, never inline JSON.

        The SDK renders a DICT-valued `ClaudeAgentOptions.mcp_servers` into
        `--mcp-config <the entire JSON>` in the child's ARGV (subprocess_cli.py, dict
        branch — the same code in 0.2.91 and in the current 0.2.128 pin, re-read at
        the ECA-138 bump). Argv is world-readable to every process of
        the same uid, so a granted lane's bearers sat in the process table for the
        whole turn — readable by that lane's own `Bash`, and by every OTHER lane
        running concurrently, granted or not. Confirmed live on mbpm2 (SDK 0.2.91,
        i.e. bundled CLI 2.1.165 — the original note said 2.1.220, which was the
        host's PATH `claude`, NOT the binary the SDK spawns; corrected under
        ECA-138): a worker turn read both planted sentinels out of its
        parent's argv, and `workers get` then returned them to the caller.

        The SDK's documented str/Path branch passes the value through as a file path
        instead, so no credential ever enters argv.

        SCOPE — what this does NOT do (be precise here; the vaguer version of this
        paragraph was itself a review finding). It does not make the credentials
        unreachable, and it establishes no boundary BETWEEN lanes: every lane runs as
        the same uid, so the file is readable by any of them, not just its owner. What
        changes is the shape of the exposure — from a whole-config blob sitting in the
        process table, which a stray `ps aux` scoops up by accident, to a named file a
        lane has to go open on purpose. Accident becomes intent; that is the entire
        gain, and it is worth having, but "cross-lane" is NOT closed.
        The file is also a WRITE target for any same-uid process, so a lane can in
        principle swap another lane's config between this write and the CLI's read.
        `_worker_loop` purges it as soon as the turn ends, so the window is turn-scoped
        on every in-process path. Not quite the property argv had — argv died with the
        process even on SIGKILL, and a SIGKILLed daemon does leave this file behind
        until the boot sweep in `start()` clears it.

        NOT ADDRESSED HERE, and larger: `state.db` stores every worker's policy —
        credentials verbatim — and that copy is durable in a way argv never was. It
        was 0644 in a 0755 directory, which made this file's 0600 buy nothing against
        a reader of another uid. FIXED in ECA-136: the whole supervisor home is now
        0700 with 0600 files, swept at boot (`hardening.harden_home`), and deletes are
        zeroed (`PRAGMA secure_delete`) with a one-shot VACUUM for pages freed before
        that. Still true, and the reason the paragraph above stands: every lane runs
        as THIS uid, so none of that is a lane-to-lane boundary.

        Returns `{}` for an un-granted lane so its options are byte-for-byte what they
        were before this change (and no `--mcp-config` reaches its argv at all).

        Raises OSError on any filesystem failure; the caller must fail the TURN rather
        than let it escape (an escape kills the worker's runner loop and wedges the lane
        silently — found in review before it ever shipped).
        """
        if not servers:
            return {}
        # The purge below validates and then SWALLOWS the refusal by design, so it
        # cannot be what protects this path: without an explicit check here, a name
        # persisted before spawn-time validation existed would plant a credential file
        # OUTSIDE the supervisor home, where neither the purge nor the boot sweep can
        # ever reach it. Round 2 fixed the traversal unlink; this is the traversal write.
        require_safe_worker_name(worker)
        self._purge_mcp_config(worker)  # a retry/restart leftover must not accumulate
        d = self._cfg.mcp_config_dir
        if d.is_symlink():
            # Following it would chmod 0700 and plant credentials in whatever it
            # points at. Same-uid, so not a privilege boundary — but the daemon
            # should not be the deputy that does it. (Only the final component is
            # guarded; a symlinked ~/.worker-supervisor itself is not.)
            raise OSError(f"{d} is a symlink; refusing to write MCP config through it")
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)  # explicit: mkdir's mode is umask-masked and skipped if it exists
        # The filename is UNPREDICTABLE and the open is exclusive, which together kill
        # the pre-planting class outright instead of enumerating link types. O_NOFOLLOW
        # alone did not: it rejects symlinks and says nothing about HARD links, and a
        # hard link planted at a derived (therefore predictable) path was demonstrated
        # taking the full credential write — O_TRUNC destroying the target, fchmod
        # setting it 0600, and the credential surviving at the attacker's path after
        # the turn-end unlink, which removes the link and not the inode. O_EXCL means
        # the daemon never opens an inode it did not just create.
        path = d / f"{worker}-{turn_id}-{secrets.token_hex(8)}.json"
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)  # nothing owns it yet; don't leak the descriptor
            raise
        # fdopen takes ownership here, and closes the fd itself if construction fails —
        # so it must NOT be inside the handler above, or a failure would double-close a
        # number another thread may already have been handed.
        with os.fdopen(fd, "w") as fh:
            json.dump({"mcpServers": servers}, fh)
        return str(path)

    async def _finish_failure_capsule(
        self,
        name: str,
        turn_id: int,
        reason: str,
        options_snapshot: dict[str, Any],
        stderr_tail: list[str],
        resume_chain: list[str | None],
        outcome: TurnOutcome | None = None,
    ) -> None:
        """ECA-143 review round: this takes the OUTCOME, not a pre-built diagnostics
        dict, so `diagnostics()` is called INSIDE the try below.

        Arguments are evaluated before the call, so a `diagnostics=outcome.diagnostics()`
        at a call site sat outside this handler: a raise in there would escape
        `_run_turn` past `set_worker_status(name, "idle")` and kill the runner loop —
        the ECA-135/137 lane-wedge class, and precisely the trap `write_capsule`'s own
        docstring records for `events_tail`. `None` means the caller had no outcome
        (the pre-spawn budget refusal), which is different from an outcome with
        nothing in it.
        """
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
                result_diagnostics=outcome.diagnostics() if outcome else None,
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
        # ECA-147: counted before any type dispatch, so types this method does not model
        # still register as "the CLI sent us something" — `RateLimitEvent` and
        # `system/api_retry` both arrive here and are otherwise invisible. Bounded by
        # what the SDK's parser accepts (a frame it drops never reaches us), so this is a
        # diagnostic count of delivered messages, not of bytes on the wire.
        outcome.frames += 1
        if isinstance(msg, ResultMessage):
            outcome.saw_result = True
            outcome.session_id = msg.session_id
            outcome.result_text = msg.result
            outcome.is_error = bool(msg.is_error)
            outcome.cost_usd = msg.total_cost_usd
            outcome.duration_ms = msg.duration_ms
            outcome.num_turns = msg.num_turns
            # ECA-143: the SDK pin is exact (claude-agent-sdk==0.2.128), so these
            # are plain attribute reads — a getattr default would silently turn a
            # future SDK rename into "no diagnostics" again, which is the bug.
            outcome.subtype = msg.subtype
            outcome.api_error_status = msg.api_error_status
            outcome.errors = _as_error_list(msg.errors)
            outcome.terminal_reason = msg.terminal_reason
            outcome.stop_reason = msg.stop_reason
            if outcome.usage is None:  # cumulative fallback; see CONTEXT_WINDOW_TOKENS
                outcome.usage = msg.usage
        elif isinstance(msg, AssistantMessage):
            if msg.usage:
                outcome.usage = msg.usage  # last request wins: current context size
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    outcome.tools.append(block.name)
                    self._events.emit(
                        name, "tool_use", turn_id=turn_id, tool=block.name
                    )
        elif isinstance(msg, SystemMessage) and msg.subtype == "init":
            # ECA-147: the CLI's announcement that a session is live. On a resumed
            # turn it is also the proof the chain was ACCEPTED — see
            # _is_dead_resume_chain, which is the whole of G7's trigger.
            outcome.saw_init = True
            # ECA-101 diagnostic: a point-in-time snapshot taken at turn start —
            # non-'sdk' servers are commonly still "pending" here (the CLI never
            # waits on them), so this does NOT prove a server never connected,
            # only what its state was at this instant.
            outcome.mcp_init = msg.data.get("mcp_servers")

    async def _after_turn(self, name: str, turn_id: int) -> None:
        """Lifecycle chaining once a turn reaches a terminal state."""
        turn = await self._reg.get_turn(turn_id)
        if turn is None or turn["state"] != "done":
            return  # keep-on-failure: no auto-progression past a failed turn
        kind = turn["kind"]
        worker = await self._reg.get_worker(name)
        repo = worker["repo"] if worker else ""
        if kind == "cycle_handover":
            epoch = await self._reg.roll_epoch(name, "cycled")
            await self._reg.enqueue_turn(name, restore_prompt(repo), kind="restore")
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
        if kind == "restore":
            # Bounded restore (ECA-84): the restore turn only RE-GROUNDS (see
            # restore_prompt) — it does not carry out the work. Auto-enqueue ONE
            # continuation work-turn so autonomous work still proceeds, under a
            # FRESH wall-clock/budget and as kind='prompt' so context-pressure
            # auto-cycle re-arms (it never fires on a 'restore' turn). Guard on an
            # empty queue: a manual cycle where the orchestrator already queued its
            # own next prompt must not get a racing continuation stacked behind it.
            if await self._reg.next_queued_turn(name) is None:
                await self._reg.enqueue_turn(name, continue_prompt(), kind="prompt")
                self._events.emit(name, "restore_continued")
                self._kick(name)
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
            await self._reg.enqueue_turn(name, cycle_prompt(repo), kind="cycle_handover")
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
