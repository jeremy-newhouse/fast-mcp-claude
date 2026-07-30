"""Turn engine behavior against a scripted fake `query`: resume chaining, the
failure ladder (retry-once / resume_failed), budget refusal, cycling (manual +
auto), and lifecycle records — the code-provable halves of AC-WS-1/4/5/11."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
)

from worker_supervisor.engine import (
    STOP_GRACE_S,
    Engine,
    TurnOutcome,
    _discipline_append,
    _is_dead_resume_chain,
)
from worker_supervisor.config import Limits
from worker_supervisor.gate import QuestionBridge, WorkerPolicy
from worker_supervisor.names import DAEMON_KEY
from worker_supervisor.registry import TURN_TERMINAL


def r(session_id: str, *, cost: float = 0.01, usage: dict | None = None,
      is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=90,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=cost,
        usage=usage or {"input_tokens": 1000, "cache_read_input_tokens": 0},
        result=f"result from {session_id}",
    )


def r_api_error(
    session_id: str = "sERR",
    *,
    status: int | None = 429,
    errors: list[str] | None = None,
    text: str = "You've hit your monthly spend limit",
    cost: float = 0.0,
) -> ResultMessage:
    """The ECA-143 result frame: is_error with subtype "success".

    Not a contrived combination — the SDK documents `api_error_status` as populated
    exactly when `is_error` is True and `subtype` == "success", and this is the
    frame a live spend-capped lane on mbpm2 produced (429, zero tokens, 354ms).
    """
    return ResultMessage(
        subtype="success",
        duration_ms=354,
        duration_api_ms=0,
        is_error=True,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=cost,
        usage={"input_tokens": 0, "output_tokens": 0},
        result=text,
        errors=errors,
        api_error_status=status,
    )


def a(*tools: str, usage: dict | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id=f"t-{t}", name=t, input={}) for t in tools],
        model="test-model",
        usage=usage,
    )


def SDK_ERROR_EXIT() -> Exception:
    """What the SDK actually raises when the CLI exits non-zero after an error result.

    A BARE `Exception` carrying the CLI's own text — see the note in
    `make_fake_query`. Named rather than inlined so no test quietly reverts to
    `ProcessError` and ends up exercising a clause production never reaches.
    """
    return Exception("Claude Code returned an error result: success")


class Hang:
    """Script sentinel: the stream stalls here and never completes.

    Placed AFTER a result frame it expresses the shape the SDK explicitly supports
    (a result arrives, stdin stays open for in-flight work) — which is how a turn
    can hit the wall clock with a ResultMessage already observed.
    """


def init(**data: Any) -> SystemMessage:
    return SystemMessage(subtype="init", data=data)


class Refused:
    """Script sentinel: the CLI refused the session it was told to resume.

    The full measured shape (CLI 2.1.220), not just the exception: the refusal goes to
    STDERR, no message frame is emitted, and the process exits. Which exception the SDK
    then delivers depends on where it noticed the exit, so that is a parameter here —
    but the stderr line is the CLI's own, and it is the same either way. G7 needs one of
    the two as corroboration, so a script that raises without the stderr line describes
    a DIFFERENT failure (an environment one) and must not be mistaken for this.
    """

    def __init__(self, exc: Exception, session_id: str = "s1") -> None:
        self.exc = exc
        self.stderr = f"No conversation found with session ID: {session_id}"


class Stderr:
    """Script sentinel: write a line to the turn's stderr instead of yielding a frame.

    Lets a script put the CLI's refusal text on stderr for a turn that DID start, which
    is the adversarial case for G7's narrowness: corroboration present, init present, so
    the absence test is the only thing standing between an ordinary failure and a
    discarded epoch.
    """

    def __init__(self, line: str) -> None:
        self.line = line


def make_fake_query(script: list[Any], calls: list[Any]):
    async def fake_query(*, prompt, options, transport=None):
        idx = len(calls)
        calls.append(options)
        item = script[idx] if idx < len(script) else script[-1]
        if isinstance(item, Refused):
            # stderr FIRST: the CLI writes its complaint and then exits, so the tail is
            # already populated by the time the SDK surfaces an exception.
            if options.stderr is not None:
                options.stderr(item.stderr)
            raise item.exc
        if isinstance(item, Exception):
            # A bare raise is the DIED-BEFORE-ANY-FRAME shape, and on the real CLI
            # that is what a rejected resume looks like: measured on 2.1.220, a
            # resume the CLI refuses produces zero frames and stderr "No
            # conversation found with session ID: <id>". ECA-147 keys G7 on exactly
            # that absence, so this distinction is now load-bearing — an Exception
            # INSIDE the message list (yield-then-raise) is a different failure.
            raise item
        async for _ in prompt:  # consume the stream like the SDK does
            break
        if options.stderr is not None:
            options.stderr("mock cli stderr line")
        # ECA-147: the real CLI opens EVERY turn — fresh or resumed — with
        # SystemMessage(subtype="init"), probed on 2.1.220. A script that omits it
        # describes a stream the SDK never produces, which is how G7 came to be
        # tested only through a shape production never reaches. Scripts that supply
        # their own init frame (the ECA-101 mcp_servers snapshot) keep it.
        if not (item and isinstance(item[0], SystemMessage) and item[0].subtype == "init"):
            item = [init()] + list(item)
        for msg in item:
            # An Exception INSIDE the message list means yield-then-raise, which is
            # the real ECA-143 shape: the CLI emits a result frame with
            # is_error=True and only THEN exits non-zero, so the SDK surfaces an
            # exception for a turn whose result was already observed. A script that
            # can only raise INSTEAD of yielding cannot express that at all.
            #
            # Use a BARE `Exception` for that, not ProcessError. The SDK swallows the
            # transport's ProcessError (`_internal/query.py` catches it, replaces it
            # with the CLI's own error text, and re-sends it as `{"type": "error"}`),
            # and `receive_messages` then raises a bare `Exception`. Confirmed by the
            # live ECA-143 capsule, whose recorded reason begins "Exception: " — the
            # engine's ProcessError clause would have written "ProcessError: ".
            if isinstance(msg, Stderr):
                if options.stderr is not None:
                    options.stderr(msg.line)
                continue
            if isinstance(msg, Exception):
                raise msg
            if isinstance(msg, Hang):
                await asyncio.sleep(3600)  # the wall-clock timeout owns this turn
            yield msg

    return fake_query


@pytest.fixture
async def make_engine(cfg, registry, events, monkeypatch):
    engines: list[Engine] = []

    def _make(script: list[Any]):
        calls: list[Any] = []
        monkeypatch.setattr("worker_supervisor.engine.query", make_fake_query(script, calls))
        # Fake sessions never hit the real cwd-keyed store.
        monkeypatch.setattr(Engine, "_transcript_exists", lambda self, cwd, sid: True)
        bridge = QuestionBridge(registry, events)
        engine = Engine(cfg, registry, events, bridge)
        engines.append(engine)
        return engine, calls

    yield _make
    for e in engines:
        await e.stop()


async def wait_until(predicate, timeout: float = 5.0):
    async def _poll():
        while True:
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout)


async def terminal_turn(registry, turn_id: int, timeout: float = 5.0):
    async def _check():
        t = await registry.get_turn(turn_id)
        return t if t and t["state"] in TURN_TERMINAL else None

    return await wait_until(_check, timeout)


async def capsule_paths(cfg, worker: str, turn_id: int | None = None, timeout: float = 5.0):
    """Capsule files for `worker`, WAITED for rather than globbed (ECA-150).

    `terminal_turn` returns the instant `finish_turn` commits, and every failure
    path writes its capsule after that — so globbing straight afterwards races the
    runner and, under full-suite load, finds an empty directory. Same window
    ECA-148 traced (observing a row before `_run_turn`'s tail finishes), different
    victim. Measured at roughly 2 runs in 10 before this helper existed.
    """
    pattern = (
        f"{worker}-turn{turn_id}-*.json" if turn_id is not None else f"{worker}-turn*.json"
    )

    async def _found():
        return sorted(cfg.capsules_dir.glob(pattern)) or None

    try:
        return await wait_until(_found, timeout)
    except (asyncio.TimeoutError, TimeoutError):
        raise AssertionError(
            f"no capsule matching {pattern!r} appeared within {timeout}s "
            f"in {cfg.capsules_dir}"
        ) from None



async def test_happy_turn_persists_session_and_telemetry(make_engine, registry, repo):
    engine, calls = make_engine([[a("Read", "Bash"), r("s1", cost=0.05)]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "do the thing")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "done"
    assert turn["session_id"] == "s1"
    assert turn["cost_usd"] == pytest.approx(0.05)
    assert "Read" in turn["tools"] and "Bash" in turn["tools"]
    worker = await registry.get_worker("w1")
    assert worker["status"] == "idle"
    assert calls[0].resume is None
    assert calls[0].setting_sources == ["project"]


async def test_turns_chain_via_resume(make_engine, registry, repo):
    engine, calls = make_engine([[r("s1")], [r("s2")]])
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "one")
    await terminal_turn(registry, t1)
    t2 = await engine.prompt("w1", "two")
    turn2 = await terminal_turn(registry, t2)
    assert turn2["session_id"] == "s2"
    assert calls[1].resume == "s1"  # the epoch chain


async def test_bare_exception_retries_once_then_succeeds(make_engine, registry, repo, events):
    """G2: mid-stream death is a bare Exception; rebuild + retry once, same resume."""
    engine, calls = make_engine([RuntimeError("subprocess died mid-stream"), [r("s1")]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "flaky")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "done" and turn["session_id"] == "s1"
    assert len(calls) == 2
    assert any(e["event"] == "turn_retry" for e in events.read("w1"))


async def test_second_failure_is_terminal_with_capsule(make_engine, registry, repo, cfg):
    engine, _ = make_engine([RuntimeError("boom 1"), RuntimeError("boom 2"), [r("sX")]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "doomed")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "error" and "boom 2" in turn["error"]
    capsules = await capsule_paths(cfg, "w1")
    assert capsules, "failure capsule missing (Amendment A6)"
    epoch = await registry.current_epoch("w1")
    assert epoch["seq"] == 1 and epoch["ended_at"] is None  # keep-on-failure


async def test_api_error_failure_keeps_the_clis_own_account_of_it(
    make_engine, registry, repo, cfg, events
):
    """ECA-143: the failure path must persist what the CLI reported, not less.

    Live shape being reproduced: a lane turn on mbpm2 failed twice, `stderr_tail`
    was `[]`, and the only recorded reason was the SDK's own string "Claude Code
    returned an error result: success" — which names no cause and contradicts
    itself. The CLI had reported HTTP 429 (a monthly spend cap) and every field
    carrying that was dropped by `_fail_turn`. The give-away was a `duration_ms`
    and a `usage` blob in the capsule beside `result_text: null`: a ResultMessage
    had plainly been observed.
    """
    boom = SDK_ERROR_EXIT()
    engine, calls = make_engine([[r_api_error(), boom], [r_api_error(), boom]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "spend-capped")
    turn = await terminal_turn(registry, tid)
    # The bare-Exception path, which is the one the live SDK reaches for a failure
    # AFTER the session started — the engine's own error string proves it.
    assert turn["error"].startswith("Exception: ")

    assert turn["state"] == "error"
    # The three fields the success path kept and this one silently discarded.
    assert turn["result_text"] == "You've hit your monthly spend limit"
    assert turn["is_error"] == 1
    assert turn["num_turns"] == 1

    capsule = json.loads(
        (await capsule_paths(cfg, "w1"))[-1].read_text(encoding="utf-8")
    )
    diag = capsule["result_diagnostics"]
    assert diag["api_error_status"] == 429  # the field that makes the failure a diagnosis
    assert diag["subtype"] == "success" and diag["is_error"] is True
    assert diag["saw_result"] is True
    assert diag["saw_init"] is True, "ECA-147: this failure came AFTER the session started"
    assert diag["frames"] >= 2, f"the init and result frames were not counted: {diag}"

    # `workers events` is read before any capsule is opened, so the status has to
    # be visible there too.
    rows = events.read("w1")
    errored = [e for e in rows if e["event"] == "turn_error"]
    assert errored and errored[-1]["api_error_status"] == 429

    # ECA-147, AC#4: and it is NOT retried. This test used to assert a `turn_retry`
    # carrying the 429 — the ladder really did rebuild the subprocess and re-spend
    # the turn against a monthly spend cap, with no backoff, on every turn. A second
    # attempt cannot raise a quota, so the retry is pure load; the suppression has to
    # be RECORDED, because a missing turn_retry row is otherwise indistinguishable
    # from a ladder that was never wired up.
    assert not [e for e in rows if e["event"] == "turn_retry"], "a 429 must not be retried"
    assert len(calls) == 1, "the CLI was invoked twice for a non-retryable failure"
    assert "api_error_status=429" in errored[-1]["no_retry"]


async def test_failure_with_no_result_frame_says_so_rather_than_inventing_a_status(
    make_engine, registry, repo, cfg, events
):
    """ECA-143: absence is evidence, and must not be reported as a status.

    A crash before any result frame is a DIFFERENT failure than an API error, and
    the capsule has to let an operator tell them apart: `saw_result: false` and a
    null status. The event carries no `api_error_status` KEY at all rather than a
    null one — the event log is append-only and read by eye.
    """
    engine, _ = make_engine([RuntimeError("boom 1"), RuntimeError("boom 2"), [r("sX")]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "no result frame")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "error"

    capsule = json.loads(
        (await capsule_paths(cfg, "w1"))[-1].read_text(encoding="utf-8")
    )
    diag = capsule["result_diagnostics"]
    assert diag is not None, "the key must exist, or the reader assumes an old capsule"
    assert diag["saw_result"] is False
    assert diag["api_error_status"] is None and diag["subtype"] is None

    errored = [e for e in events.read("w1") if e["event"] == "turn_error"]
    assert errored and "api_error_status" not in errored[-1]


async def test_timeout_path_keeps_the_result_it_had_observed(
    make_engine, registry, repo, cfg
):
    """ECA-143: the wall-clock path dropped the same three fields as the error path.

    A turn that reached the wall clock AFTER a result frame is the case where that
    loss hurts most: the reason it then stalled is often in the result the CLI had
    already sent, and 'wall clock exceeded (Ns)' says nothing about it.
    """
    engine, _ = make_engine([[r_api_error(), Hang()]])
    await engine.spawn("w1", str(repo), WorkerPolicy(limits={"wall_clock_s": 1}))
    tid = await engine.prompt("w1", "stalls after the result")
    turn = await terminal_turn(registry, tid, timeout=15.0)

    assert turn["state"] == "timeout"
    assert turn["result_text"] == "You've hit your monthly spend limit"
    assert turn["is_error"] == 1 and turn["num_turns"] == 1
    capsule = json.loads(
        (await capsule_paths(cfg, "w1", tid))[-1].read_text(encoding="utf-8")
    )
    assert capsule["result_diagnostics"]["api_error_status"] == 429


async def test_timeout_row_still_charges_the_epoch_for_what_the_turn_spent(
    make_engine, registry, repo
):
    """ECA-143 review round: the timeout path dropped SIX fields, not three.

    `finish_turn` writes every column unconditionally and the epoch's spend is
    incremented only from the `cost_usd` handed to it, so a turn that burned money
    and THEN hit the wall clock was charged nothing against max_budget_usd_per_epoch
    — and wall-clock breaches are exactly the expensive turns. `workers status` also
    reported a null context_pct, because it reads the last finished turn's usage.
    """
    engine, _ = make_engine([[r("s1", cost=0.05, usage={"input_tokens": 20_000}), Hang()]])
    await engine.spawn("w1", str(repo), WorkerPolicy(limits={"wall_clock_s": 1}))
    tid = await engine.prompt("w1", "expensive, then stalls")
    turn = await terminal_turn(registry, tid, timeout=15.0)

    assert turn["state"] == "timeout"
    assert turn["cost_usd"] == 0.05
    assert turn["duration_ms"] == 100 and json.loads(turn["usage"])["input_tokens"] == 20_000
    epoch = await registry.current_epoch("w1")
    assert epoch["cost_usd"] == 0.05, "spend escaped the epoch budget cap"
    # `status` reads context_pct off the last finished turn's usage, so dropping
    # `usage` here also blinded the operator's only context-pressure signal.
    assert (await engine.status())[0]["context_pct"] == 10


async def test_resume_failed_capsule_says_the_session_never_started(
    make_engine, registry, repo, cfg, events
):
    """What a dead resume actually records — and why its old script was fiction.

    This test used to script `[result_frame, ProcessError]`: a turn that observed a
    429 result and THEN got a ProcessError, asserting the resume_failed row kept all
    six telemetry fields (the ECA-143 spend-leak fix on this path). ECA-147 measured
    the real thing and that shape does not exist. A rejected resume dies with ZERO
    frames — so there is no result to keep, and `saw_result: false` /
    `saw_init: false` is the honest content of the capsule. Nor was the docstring's
    caveat right: `except ProcessError` was not unreachable, it was reachable ONLY
    here (the SDK re-raises the raw transport error to a pending `initialize`), which
    is why the branch appeared to work while the invariant it encodes did not hold
    for the other two exception types.

    The six-field pass-through stays in the code as a defensive write (`finish_turn`
    has no COALESCE), but it is no longer load-bearing and this test no longer claims
    it is. What IS load-bearing: an operator opening this capsule can tell "the chain
    was refused" from "the turn ran and then broke".

    Deliberately a bare `ProcessError` rather than the fuller `Refused` shape: with no
    refusal line on stderr, the non-zero exit is the ONLY corroboration available, so
    this is the test that keeps that route alive on its own.
    """
    engine, _ = make_engine(
        [[r("s1")], ProcessError("Command failed with exit code 1", exit_code=1), [r("s3")]]
    )
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "one")
    await terminal_turn(registry, t1)
    t2 = await engine.prompt("w1", "two")
    turn2 = await terminal_turn(registry, t2)

    assert turn2["state"] == "error" and "resume failed" in turn2["error"]
    # The type and the exit code both survive into the row: on a rejected resume the
    # CLI's own words are in stderr, and the exit code is all the exception carries.
    assert "ProcessError" in turn2["error"] and "exit=1" in turn2["error"]
    capsule = json.loads(
        (await capsule_paths(cfg, "w1", t2))[-1].read_text(encoding="utf-8")
    )
    diag = capsule["result_diagnostics"]
    assert diag["saw_init"] is False and diag["frames"] == 0
    assert diag["saw_result"] is False and diag["api_error_status"] is None
    failed = [e for e in events.read("w1") if e["event"] == "resume_failed"]
    assert failed and failed[-1]["resume"] == "s1"
    assert "api_error_status" not in failed[-1]  # absence is not a status (ECA-143)


@pytest.mark.parametrize(
    "event,script,status",
    [
        # The retry path, which ECA-147 narrowed: it is reached by a 5xx (an
        # overloaded upstream can genuinely succeed on a second attempt) and NOT by
        # the 4xx this test used to use — see test_api_error_failure_... for that.
        ("turn_retry", [[r_api_error(status=503), SDK_ERROR_EXIT()], [r("sOK")]], 503),
        ("turn_timeout", [[r_api_error(), Hang()]], 429),
        # is_error result, CLI exits 0: no exception anywhere, so the event is the
        # ONLY place an operator sees a reason at all.
        ("turn_finished", [[r_api_error()]], 429),
    ],
)
async def test_every_failure_event_carries_the_api_status(
    make_engine, registry, repo, events, event, script, status
):
    """ECA-143 review round: five of the new event fields had no falsifying test,
    including the retry clause the live SDK actually reaches — so the suite proved
    the status reached a path production never takes."""
    engine, _ = make_engine(script)
    await engine.spawn("w1", str(repo), WorkerPolicy(limits={"wall_clock_s": 1}))
    tid = await engine.prompt("w1", "api error")
    await terminal_turn(registry, tid, timeout=15.0)

    rows = [e for e in events.read("w1") if e["event"] == event]
    assert rows, f"no {event} event"
    assert rows[-1]["api_error_status"] == status


async def test_capsule_bounds_the_clis_error_list(make_engine, registry, repo, cfg):
    """ECA-143: `errors` is the one diagnostics field that can carry text, so it is
    bounded. A capsule too large to open is not evidence."""
    boom = SDK_ERROR_EXIT()
    flood = [f"{i}:" + "x" * 2000 for i in range(50)]
    engine, _ = make_engine(
        [[r_api_error(errors=flood), boom], [r_api_error(errors=flood), boom]]
    )
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "flood")
    await terminal_turn(registry, tid)

    capsule = json.loads(
        (await capsule_paths(cfg, "w1"))[-1].read_text(encoding="utf-8")
    )
    errors = capsule["result_diagnostics"]["errors"]
    assert len(errors) == 10
    assert all(len(e) == 500 for e in errors)
    assert errors[0].startswith("0:")  # bounded from the FRONT: the first error caused the rest


async def test_a_non_list_errors_field_cannot_fail_a_successful_turn(
    make_engine, registry, repo
):
    """ECA-143 review round: `list(msg.errors)` on a non-iterable raised inside
    `_observe`, i.e. inside the message loop — so a turn the CLI reported as DONE
    became a supervisor error whose recorded reason blamed the supervisor. A
    diagnostic must not be able to fail the thing it is describing."""
    good = r("s1")
    good.errors = 5  # a protocol violation; message_parser validates nothing here
    engine, _ = make_engine([[good]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "fine, but malformed errors")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "done", turn["error"]


async def test_a_string_errors_field_is_not_split_into_characters(
    make_engine, registry, repo, cfg
):
    """ECA-143 review round: a str is Iterable, so `list()` exploded it per character
    and the 10-item bound then kept ten single letters — the message destroyed, in the
    one field meant to carry the CLI's own words. Mangled evidence is worse than none:
    it reads as data."""
    engine, _ = make_engine(
        [
            [r_api_error(errors="monthly spend cap"), SDK_ERROR_EXIT()],
            [r_api_error(errors="monthly spend cap"), SDK_ERROR_EXIT()],
        ]
    )
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "string errors")
    await terminal_turn(registry, tid)

    capsule = json.loads(
        (await capsule_paths(cfg, "w1"))[-1].read_text(encoding="utf-8")
    )
    assert capsule["result_diagnostics"]["errors"] == ["monthly spend cap"]


async def test_turn_mcp_diagnostics_emitted_on_failure_ladder_too(
    make_engine, registry, repo, events
):
    """ECA-101 AC1 (adversarial-review follow-up): the FAILURE ladder (_fail_turn)
    must surface MCP diagnostics too, not just a successful turn — a granted
    server that never finishes connecting is exactly the kind of failure an
    operator most wants this evidence for, and the pre-existing failure capsule
    (Amendment A6) carries no MCP-specific context at all."""
    engine, _ = make_engine([RuntimeError("boom 1"), RuntimeError("boom 2"), [r("sX")]])
    await engine.spawn(
        "w1", str(repo), WorkerPolicy(mcp_servers={"context7": {"type": "stdio"}})
    )
    tid = await engine.prompt("w1", "doomed")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "error"

    diag = [e for e in events.read("w1") if e["event"] == "turn_mcp_diagnostics"]
    assert len(diag) == 1
    assert diag[0]["granted"] == ["context7"]


@pytest.mark.parametrize(
    "boom",
    [
        # Every type the SDK can hand us for the SAME event — the CLI died before it
        # announced the session. Which one arrives depends on where inside the SDK the
        # exit was noticed, not on anything about the failure:
        #   ProcessError       — the raw transport error, re-raised to the pending
        #                        `initialize` control request. MEASURED as the shape a
        #                        rejected resume produces (SDK 0.2.128 / CLI 2.1.220).
        #   CLIConnectionError — the exit lost the race with initialize's stdin write
        #                        ("Cannot write to process that exited with error").
        #                        Read from subprocess_cli.py, not measured.
        #   Exception          — the converted `{"type": "error"}` frame, which is what
        #                        `receive_messages` raises. The ECA-143 shape.
        # G7 used to be spelled `except ProcessError`, so two of these three silently
        # skipped it. This parametrization is the regression guard: the branch must not
        # be reachable-by-type again. All three carry the CLI's refusal on stderr,
        # because that is what the real thing does (`Refused`) — and for the two
        # non-ProcessError types it is the ONLY corroboration, so this also proves the
        # stderr route works, not just the exit-code one.
        pytest.param(
            Refused(ProcessError("Command failed with exit code 1", exit_code=1)),
            id="ProcessError",
        ),
        pytest.param(
            Refused(CLIConnectionError("Cannot write to process that exited")),
            id="CLIConnectionError",
        ),
        pytest.param(Refused(Exception("Command failed with exit code 1")), id="bare-Exception"),
    ],
)
async def test_resume_failure_rolls_epoch_and_enqueues_restore(
    make_engine, registry, repo, cfg, boom
):
    """G7: a dead resumed chain -> epoch ends, restore turn grounds the next."""
    engine, calls = make_engine([[r("s1")], boom, [r("s3")]])
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "one")
    await terminal_turn(registry, t1)
    t2 = await engine.prompt("w1", "two")
    turn2 = await terminal_turn(registry, t2)
    assert turn2["state"] == "error" and "resume failed" in turn2["error"]
    # ECA-143: this capsule carries the diagnostics block too. Here the CLI died
    # before any result frame, so the honest content is "there was none".
    capsule = json.loads(
        (await capsule_paths(cfg, "w1", t2))[-1].read_text(encoding="utf-8")
    )
    assert capsule["result_diagnostics"]["saw_result"] is False

    async def _restore_done():
        rows = await registry.history("w1", limit=10)
        done = [t for t in rows if t["kind"] == "restore" and t["state"] == "done"]
        return done or None

    restore = (await wait_until(_restore_done))[0]
    assert restore["session_id"] == "s3"
    epoch = await registry.current_epoch("w1")
    assert epoch["seq"] == 2
    assert calls[2].resume is None  # fresh chain, grounded by the handover restore


REFUSAL = ["No conversation found with session ID: s1"]


@pytest.mark.parametrize(
    "resume_from,saw_init,exc,stderr,dead,why",
    [
        # The measured refusal: exited non-zero, announced nothing.
        ("s1", False, ProcessError("x", exit_code=1), [], True, "exit code corroborates"),
        # Same refusal reported only on stderr — the type is the SDK's to change.
        ("s1", False, Exception("x"), REFUSAL, True, "the CLI said so"),
        # Chain was accepted, then the turn died: ordinary failure, keep the epoch.
        ("s1", True, ProcessError("x", exit_code=1), REFUSAL, False, "session was live"),
        # No chain to lose.
        (None, False, ProcessError("x", exit_code=1), REFUSAL, False, "not resuming"),
        # Pre-init deaths that say NOTHING about the chain. Review round found these
        # rolling: a missing binary or a vanished cwd raises out of connect() before a
        # read task exists, and a 60s initialize timeout is a bare Exception with no
        # frames. Rolling on those discards a live session and re-grounds the lane from
        # a handover file that may be an epoch old.
        ("s1", False, CLINotFoundError("no claude binary"), [], False, "environment"),
        ("s1", False, CLIConnectionError("cwd is gone"), [], False, "environment"),
        ("s1", False, Exception("Control request timeout: initialize"), [], False, "slow MCP"),
    ],
)
def test_dead_resume_chain_predicate(resume_from, saw_init, exc, stderr, dead, why):
    """G7's trigger, pinned directly: absence of an init frame PLUS corroboration.

    The engine-level tests below prove the WIRING, and they are the ones that matter —
    but each reaches this decision through a full turn, so a mutation that makes it fire
    everywhere sends the lane into a roll/restore loop and those tests then HANG instead
    of failing (measured while falsifying this change). A predicate test cannot hang, so
    it is what pins the truth table.
    """
    outcome = TurnOutcome(saw_init=saw_init)
    assert _is_dead_resume_chain(resume_from, outcome, exc, stderr) is dead, why


async def test_a_resumed_turn_that_started_and_then_died_does_not_roll_the_epoch(
    make_engine, registry, repo, events
):
    """The other half of ECA-147: G7 must stay NARROW.

    The CLI accepted the chain (it announced the session, and here even produced a
    result) and then the process died. That is an ordinary turn failure — the epoch is
    alive, keep-on-failure applies, and rolling it would throw away a working session
    plus a restore turn's worth of context for nothing. This is the case the live
    ECA-143 lane hit on every turn, so an over-broad fix would have cycled that lane's
    epoch repeatedly instead of just failing the turn.

    Set up adversarially: the refusal text is on stderr TOO, so the corroboration half of
    the test is satisfied and the observed init frame is the only thing preventing a
    roll. That makes this the test that fails if `_observe` stops recording `saw_init` —
    and the one that fails if the fake stops prepending the init frame the real CLI
    always sends.
    """
    boom = SDK_ERROR_EXIT()
    started_then_died = [r("s2"), Stderr("No conversation found with session ID: sX"), boom]
    engine, _ = make_engine([[r("s1")], started_then_died, started_then_died])
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "one")
    await terminal_turn(registry, t1)
    t2 = await engine.prompt("w1", "two")
    turn2 = await terminal_turn(registry, t2)

    assert turn2["state"] == "error"
    assert "resume failed" not in (turn2["error"] or "")
    rows = events.read("w1")
    assert not [e for e in rows if e["event"] == "resume_failed"]
    epoch = await registry.current_epoch("w1")
    assert epoch["seq"] == 1 and epoch["ended_at"] is None
    assert not [t for t in await registry.history("w1", limit=10) if t["kind"] == "restore"]


async def test_a_first_turn_dying_before_init_does_not_roll_the_epoch(
    make_engine, registry, repo, events
):
    """No resume, no dead chain. G7 is about a chain we cannot prove is alive; a
    fresh turn has no chain to lose, so a pre-init death there is just a failure —
    otherwise a lane whose very first turn cannot start would roll epochs forever."""
    engine, _ = make_engine([ProcessError("Command failed with exit code 1", exit_code=1)])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "first turn ever")
    turn = await terminal_turn(registry, tid)

    assert turn["state"] == "error" and "resume failed" not in (turn["error"] or "")
    assert not [e for e in events.read("w1") if e["event"] == "resume_failed"]
    epoch = await registry.current_epoch("w1")
    assert epoch["seq"] == 1


async def test_a_failing_restore_does_not_roll_the_epoch_again(
    make_engine, registry, repo, events
):
    """The easy half of termination: the recovery's own restore turn fails.

    It cannot roll again, because the fresh epoch holds no session id, so its turns run
    with `resume_from is None` and take the ordinary ladder. NOTE what this does not
    prove — see the next test. This test used to be called "…at most once" and claimed
    to establish termination in general; the review round showed that was false, and
    that its script (every call after the first fails) was the one shape in which the
    loop cannot start.
    """
    engine, _ = make_engine(
        [[r("s1")], ProcessError("Command failed with exit code 1", exit_code=1)]
    )
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "one")
    await terminal_turn(registry, t1)
    t2 = await engine.prompt("w1", "two")
    await terminal_turn(registry, t2)

    async def _restore_terminal():
        rows = await registry.history("w1", limit=10)
        done = [
            t for t in rows if t["kind"] == "restore" and t["state"] in TURN_TERMINAL
        ]
        return done or None

    restore = (await wait_until(_restore_terminal))[0]
    assert restore["state"] == "error", "the restore turn was supposed to fail too"
    assert len([e for e in events.read("w1") if e["event"] == "resume_failed"]) == 1
    epoch = await registry.current_epoch("w1")
    assert epoch["seq"] == 2, "the epoch rolled more than once"


async def test_a_second_dead_resume_inside_a_recovery_does_not_roll_again(
    make_engine, registry, repo, events
):
    """The hard half, and the one the first round of this change got wrong.

    Systemic refusal: the CLI works, but every RESUME is refused (a read-only
    `~/.claude/projects`, or `session_transcript_path` drifting from what the CLI
    actually reads). Then G7's cure feeds itself — roll, restore (succeeds, it resumes
    nothing), auto-continue (ECA-84), that continuation resumes the restore's session,
    refused, roll again. The review round measured the unbounded version at 40 rolls /
    82 CLI invocations, each roll costing a real restore turn, and each new epoch
    getting a fresh `max_budget_usd_per_epoch` so the budget cannot bound it.

    Exactly one roll, then it stops loudly with `resume_recovery_exhausted`.
    """
    refused = ProcessError("Command failed with exit code 1", exit_code=1)
    #  fresh ok  |  resume refused  |  restore ok  |  every resume after: refused
    engine, _ = make_engine([[r("s1")], refused, [r("s2")], refused])
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "one")
    await terminal_turn(registry, t1)
    t2 = await engine.prompt("w1", "two")
    await terminal_turn(registry, t2)

    async def _exhausted():
        rows = [e for e in events.read("w1") if e["event"] == "resume_recovery_exhausted"]
        return rows or None

    await wait_until(_exhausted, timeout=10.0)
    assert len([e for e in events.read("w1") if e["event"] == "resume_failed"]) == 1, (
        "G7 rolled more than once for the same unresolved refusal"
    )
    epoch = await registry.current_epoch("w1")
    assert epoch["seq"] == 2, f"epoch rolled again (seq={epoch['seq']})"
    # The continuation is the turn that got refused inside the recovery: it must be a
    # plain error, and it must say why no roll happened.
    errored = [e for e in events.read("w1") if e["event"] == "turn_error"][-1]
    assert "not rolling the epoch a second time" in errored["error"]
    assert "operator" in errored["no_retry"]


async def test_recovery_breaker_reopens_once_a_resume_actually_works(
    cfg, registry, events, repo
):
    """The breaker's truth table, directly — and its release condition.

    A hang-proof test on purpose: the engine-level version above drives real turns, so a
    mutation that disables the breaker makes it loop instead of fail. What matters here
    is that the condition is not a permanent latch — one successfully RESUMED turn and a
    genuine dead chain months later still gets its recovery.
    """
    engine = Engine(cfg, registry, events, QuestionBridge(registry, events))
    await engine.spawn("w1", str(repo))
    assert await engine._resume_recovery_would_repeat("w1") is False, "no roll yet"

    await registry.roll_epoch("w1", "cycled")
    assert await engine._resume_recovery_would_repeat("w1") is False, (
        "a cycle is not a resume failure"
    )

    await registry.roll_epoch("w1", "resume_failed")
    assert await engine._resume_recovery_would_repeat("w1") is True

    # A `done` restore turn resumes nothing, so it proves the CLI works, not the chain.
    epoch = await registry.current_epoch("w1")
    tid = await registry.enqueue_turn("w1", "restore me", kind="restore")
    await registry.claim_turn(tid)
    await registry.start_turn(tid, None)
    await registry.finish_turn(tid, "done", session_id="sR")
    assert await engine._resume_recovery_would_repeat("w1") is True, (
        "a restore turn has no resume_from — it cannot be the proof the chain is back"
    )

    # Nor does a turn that TRIED to resume and failed. It is the same evidence that got
    # us here, so counting it would release the breaker on the strength of the problem.
    tid_bad = await registry.enqueue_turn("w1", "resume and fail", kind="prompt")
    await registry.claim_turn(tid_bad)
    await registry.start_turn(tid_bad, "sR")
    await registry.finish_turn(tid_bad, "error", error="refused again")
    assert await engine._resume_recovery_would_repeat("w1") is True, (
        "a FAILED resumed turn was counted as proof the chain recovered"
    )

    tid2 = await registry.enqueue_turn("w1", "work", kind="prompt")
    await registry.claim_turn(tid2)
    await registry.start_turn(tid2, "sR")
    await registry.finish_turn(tid2, "done", session_id="sR2")
    assert await registry.has_successful_resume(epoch["id"]) is True
    assert await engine._resume_recovery_would_repeat("w1") is False, (
        "the breaker latched after the chain demonstrably recovered"
    )
    await engine.stop()


@pytest.mark.parametrize(
    "status,attempts,retried",
    [
        (429, 1, False),  # monthly spend cap — the live ECA-143 failure
        (401, 1, False),  # auth: a second attempt has the same credential
        (503, 2, True),   # upstream overload: genuinely worth one more attempt
        (None, 2, True),  # no status at all: unchanged behavior, retry once
        # The LOWER bound, which the review round found untested: a status the CLI
        # should never pair with is_error is not an error class, and must not be read
        # as "terminal" just because it is a number.
        (200, 2, True),
    ],
)
async def test_the_ladder_retries_only_what_a_retry_could_fix(
    make_engine, registry, repo, events, status, attempts, retried
):
    """ECA-147 AC#4. By the time the CLI reports an `api_error_status` it has already
    run its own exponential backoff (measured: ten `api_retry` frames over ~190s), so
    this ladder — which rebuilds the subprocess immediately, with no backoff — can only
    re-ask a question that was just answered. A 5xx is where a fresh process plausibly
    lands differently, so it keeps its retry."""
    boom = SDK_ERROR_EXIT()
    frame = r_api_error(status=status) if status is not None else r("s1")
    script = [[frame, boom], [frame, boom]] if status is not None else [
        [r("s1"), boom], [r("s1"), boom]
    ]
    engine, calls = make_engine(script)
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "classify me")
    turn = await terminal_turn(registry, tid)

    assert turn["state"] == "error"
    assert len(calls) == attempts
    rows = events.read("w1")
    assert bool([e for e in rows if e["event"] == "turn_retry"]) is retried
    errored = [e for e in rows if e["event"] == "turn_error"][-1]
    if retried:
        assert "no_retry" not in errored, "a retried failure must not claim it was suppressed"
    else:
        assert f"api_error_status={status}" in errored["no_retry"]


async def test_a_turn_that_did_retry_never_claims_the_retry_was_suppressed(
    make_engine, registry, repo, events
):
    """The status can appear only on the SECOND attempt, and then the event lied.

    Review round: `no_retry` was computed from whichever outcome failed terminally, so a
    turn that died statuslessly (retried, correctly) and then came back 429 recorded BOTH
    a `turn_retry` row and a "this was not retried" reason. The field exists to tell a
    missing retry apart from a ladder that was never wired up, so being wrong here is
    worse than being absent.
    """
    engine, calls = make_engine(
        [RuntimeError("died with no status"), [r_api_error(status=429), SDK_ERROR_EXIT()]]
    )
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "statusless then 429")
    turn = await terminal_turn(registry, tid)

    assert turn["state"] == "error" and len(calls) == 2
    rows = events.read("w1")
    assert [e for e in rows if e["event"] == "turn_retry"], "attempt 1 should have retried"
    errored = [e for e in rows if e["event"] == "turn_error"][-1]
    assert "no_retry" not in errored, f"claimed a suppression that never happened: {errored}"
    assert errored["api_error_status"] == 429  # the status still reaches the operator


async def test_epoch_budget_refuses_next_turn(make_engine, registry, repo):
    """AC-WS-5: a breached budget terminates/refuses with the reason recorded."""
    engine, _ = make_engine([[r("s1", cost=2.0)]])  # cap is 1.0 in test cfg
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "expensive")
    await terminal_turn(registry, t1)
    t2 = await engine.prompt("w1", "should refuse")
    turn2 = await terminal_turn(registry, t2)
    assert turn2["state"] == "budget_refused"
    assert "budget exhausted" in turn2["error"]


async def test_lifecycle_turn_runs_despite_exhausted_budget(make_engine, registry, repo):
    """ECA-99: an exhausted epoch must NOT refuse a lifecycle (cycle_handover) turn —
    otherwise the lane can never cycle out (the handover-write lives in the exhausted
    epoch). The cycle runs, rolls the epoch, and its SDK budget floors to the lifecycle
    reserve instead of the $0.01 no-op floor a normal over-budget turn would get."""
    engine, calls = make_engine([[r("s1", cost=2.0)], [r("s2")], [r("s3")]])  # cap is 1.0
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "expensive")
    await terminal_turn(registry, t1)  # epoch now exhausted (2.0 > 1.0 cap)
    await engine.cycle("w1")

    async def _cycled():
        epoch = await registry.current_epoch("w1")
        return epoch if epoch["seq"] == 2 else None

    await wait_until(_cycled)  # hangs (timeout) if the cycle_handover was budget_refused

    cycle_turns = [
        t for t in await registry.history("w1", limit=10) if t["kind"] == "cycle_handover"
    ]
    assert cycle_turns and cycle_turns[0]["state"] == "done"
    assert calls[1].max_budget_usd == pytest.approx(5.0)  # lifecycle reserve, not 0.01


async def test_remove_requires_terminal_then_frees_name(make_engine, registry, repo):
    """ECA-99: engine.remove refuses a live worker; after kill it purges the row so the
    same name re-spawns (kill alone keeps the PK row and blocks respawn)."""
    engine, _ = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo))
    with pytest.raises(ValueError, match="kill it before remove"):
        await engine.remove("w1")  # idle worker is live — refused
    await engine.kill("w1")
    await engine.remove("w1")
    assert await registry.get_worker("w1") is None
    again = await engine.spawn("w1", str(repo))  # name freed
    assert again["name"] == "w1" and again["status"] == "idle"


async def test_manual_cycle_rolls_epoch_restores_and_continues(make_engine, registry, repo, events):
    # ECA-84: the restore turn only re-grounds; the supervisor then auto-enqueues one
    # continuation work-turn (kind='prompt') so autonomous work proceeds under a fresh
    # budget. Expected chain: prompt -> cycle_handover -> restore -> prompt (continuation).
    engine, _ = make_engine([[r("s1")], [r("s2")], [r("s3")], [r("s4")]])
    await engine.spawn("w1", str(repo))
    t1 = await engine.prompt("w1", "work")
    await terminal_turn(registry, t1)
    await engine.cycle("w1")

    async def _cycled():
        epoch = await registry.current_epoch("w1")
        return epoch if epoch["seq"] == 2 else None

    await wait_until(_cycled)

    async def _continued():
        rows = list(reversed(await registry.history("w1", limit=10)))
        kinds = [t["kind"] for t in rows]
        if kinds == ["prompt", "cycle_handover", "restore", "prompt"] and rows[-1]["state"] == "done":
            return kinds
        return None

    kinds = await wait_until(_continued)
    assert kinds == ["prompt", "cycle_handover", "restore", "prompt"]
    assert any(e["event"] == "epoch_cycled" for e in events.read("w1"))
    assert any(e["event"] == "restore_continued" for e in events.read("w1"))


async def test_restore_continuation_is_guarded_by_a_pending_queue(
    make_engine, registry, repo, events, monkeypatch
):
    """ECA-84: a bounded restore auto-enqueues ONE continuation only when the queue is
    empty. If the orchestrator already queued its own next prompt, we must not stack a
    racing continuation behind it. Driven by calling _after_turn directly with _kick
    stubbed, so the assertion is on the enqueue decision, not on loop timing."""
    engine, _ = make_engine([[r("s1")]])
    monkeypatch.setattr(engine, "_kick", lambda name: None)  # isolate the enqueue decision
    await engine.spawn("w1", str(repo))

    async def _finished_restore() -> int:
        tid = await registry.enqueue_turn("w1", "restore", kind="restore")
        await registry.claim_turn(tid)
        await registry.start_turn(tid, None)
        await registry.finish_turn(tid, "done", session_id=f"s{tid}")
        return tid

    # Empty queue -> exactly one continuation prompt is enqueued.
    await engine._after_turn("w1", await _finished_restore())
    queued = [
        t for t in await registry.history("w1", limit=20)
        if t["kind"] == "prompt" and t["state"] == "queued"
    ]
    assert len(queued) == 1
    assert any(e["event"] == "restore_continued" for e in events.read("w1"))

    # Non-empty queue (the continuation above is still pending) -> a second bounded
    # restore adds NOTHING; the guard defers to the already-pending work.
    before = len(await registry.history("w1", limit=50))
    await _finished_restore()
    after = len(await registry.history("w1", limit=50))
    assert after == before + 1  # only the restore row itself; no extra continuation
    assert sum(e["event"] == "restore_continued" for e in events.read("w1")) == 1


async def test_resume_skips_unpersisted_session(cfg, registry, events, repo, monkeypatch):
    """Gotcha observed on CLI 2.1.165 (see _pick_resume_target; not re-measured on
    the 2.1.220 bundle): a reported session id may never reach disk — resume the
    newest PERSISTED id instead of erroring the whole epoch."""
    calls: list[Any] = []
    monkeypatch.setattr(
        "worker_supervisor.engine.query", make_fake_query([[r("s1")], [r("s2")], [r("s3")]], calls)
    )
    monkeypatch.setattr(
        Engine, "_transcript_exists", lambda self, cwd, sid: sid != "s2"  # s2 lost the race
    )
    from worker_supervisor.gate import QuestionBridge as QB

    engine = Engine(cfg, registry, events, QB(registry, events))
    try:
        await engine.spawn("w1", str(repo))
        for prompt in ("one", "two", "three"):
            tid = await engine.prompt("w1", prompt)
            await terminal_turn(registry, tid)
        assert calls[1].resume == "s1"
        assert calls[2].resume == "s1"  # s2 never persisted -> skipped
        assert any(e["event"] == "resume_target_skipped" for e in events.read("w1"))
    finally:
        await engine.stop()


def test_session_transcript_path_sanitization():
    from worker_supervisor.engine import session_transcript_path

    p = session_transcript_path("/private/tmp/my_repo.x", "abc-123")
    assert p.name == "abc-123.jsonl"
    assert p.parent.name == "-private-tmp-my-repo-x"


async def test_auto_cycle_fires_on_context_pressure(make_engine, registry, repo, events):
    """FR-WS6: usage above the threshold auto-enqueues a cycle after a clean turn."""
    big = {"input_tokens": 150_000, "cache_read_input_tokens": 50_000}
    engine, _ = make_engine([[r("s1", usage=big)], [r("s2")], [r("s3")]])
    await engine.spawn("w1", str(repo))
    await engine.prompt("w1", "heavy context work")

    async def _cycled():
        epoch = await registry.current_epoch("w1")
        return epoch if epoch["seq"] == 2 else None

    await wait_until(_cycled)
    assert any(e["event"] == "auto_cycle" for e in events.read("w1"))


async def test_context_pressure_uses_last_request_usage(make_engine, registry, repo, events):
    """Pressure reads the LAST AssistantMessage's per-request usage, never
    ResultMessage's cumulative sum — a multi-call turn's sum can exceed the
    whole context window and would thrash auto-cycle (proven live)."""
    cumulative = {"input_tokens": 100, "cache_read_input_tokens": 322_000}
    last_request = {"input_tokens": 10, "cache_read_input_tokens": 40_000}
    engine, _ = make_engine(
        [[a("Bash", usage={"input_tokens": 5, "cache_read_input_tokens": 20_000}),
          a("Read", usage=last_request),
          r("s1", usage=cumulative)]]
    )
    await engine.spawn("w1", str(repo))
    turn_id = await engine.prompt("w1", "multi tool-call turn")

    async def _done():
        turn = await registry.get_turn(turn_id)
        return turn if turn["state"] == "done" else None

    turn = await wait_until(_done)
    import json as _json

    assert _json.loads(turn["usage"]) == last_request
    finished = [e for e in events.read("w1") if e["event"] == "turn_finished"]
    assert finished[-1]["context_pct"] == 20  # 40k/200k, not min(100, 322k/200k)
    assert not any(e["event"] == "auto_cycle" for e in events.read("w1"))


async def test_system_prompt_carries_live_limits(make_engine, registry, repo, cfg):
    """ClaudeAgentOptions.system_prompt must render live wall_clock_s / max_turns /
    cycle_context_pct so the agent can self-pace — never hardcoded (ECA-72 AC#2).

    Evidence: epoch-2 restores grounded at 69-79% context because the agent had no
    per-turn awareness of its limits; epoch-3 landed 44-45% under explicit guidance.
    """
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "check options")
    await terminal_turn(registry, tid)

    sp = calls[0].system_prompt
    assert sp is not None, "system_prompt must be set on every turn"
    assert sp["type"] == "preset"
    assert sp["preset"] == "claude_code"
    append = sp["append"]

    # Discriminating substrings: the exact phrases _discipline_append renders.
    assert f"{cfg.limits.wall_clock_s}s wall-clock" in append
    assert f"{cfg.limits.max_turns} SDK turns" in append
    assert str(cfg.cycle_context_pct) in append

    # The four discipline clauses must be present.
    assert "Commit completed work BEFORE" in append
    assert "nohup" in append
    assert "Run allowlisted commands" in append
    assert "PLAINLY" in append

    # No MCP grant on this lane (default WorkerPolicy()) -> no MCP clause at all.
    assert "MCP SERVERS" not in append


def test_discipline_append_mcp_retry_hint():
    """ECA-101 AC3 mitigation: a granted-MCP lane's system prompt must tell the
    agent that a first ToolSearch/tool-call miss for one of ITS granted servers
    can be the startup race, not real unavailability, and to retry once."""
    limits = Limits(wall_clock_s=100, max_turns=10)

    no_mcp = _discipline_append(limits, 80, [])
    assert "MCP SERVERS" not in no_mcp

    with_mcp = _discipline_append(limits, 80, ["context7", "langfuse"])
    assert "MCP SERVERS" in with_mcp
    assert "context7, langfuse" in with_mcp
    assert "retry once" in with_mcp


async def test_turn_mcp_diagnostics_emitted_on_success(make_engine, registry, repo, events):
    """ECA-101 AC1: previously only a FAILED turn's stderr/mcp state ever reached
    a capsule (_finish_failure_capsule) — a turn that SUCCEEDS while a granted MCP
    server never finished connecting left zero evidence anywhere. A 'done' turn on
    an MCP-granted lane must now surface the init snapshot + raw stderr tail too."""
    init_msg = SystemMessage(
        subtype="init", data={"mcp_servers": [{"name": "context7", "status": "pending"}]}
    )
    engine, calls = make_engine([[init_msg, r("s1")]])
    await engine.spawn(
        "w1", str(repo), WorkerPolicy(mcp_servers={"context7": {"type": "stdio"}})
    )
    tid = await engine.prompt("w1", "do the thing")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "done"

    diag = [e for e in events.read("w1") if e["event"] == "turn_mcp_diagnostics"]
    assert len(diag) == 1
    assert diag[0]["granted"] == ["context7"]
    assert diag[0]["mcp_init"] == [{"name": "context7", "status": "pending"}]
    assert diag[0]["stderr_tail"] == ["mock cli stderr line"]

    # The lane's own system prompt must carry the retry-hint naming ITS servers.
    append = calls[0].system_prompt["append"]
    assert "MCP SERVERS" in append and "context7" in append


async def test_turn_mcp_diagnostics_absent_without_mcp_grant(make_engine, registry, repo, events):
    """A lane with no MCP grant at all gets no diagnostics noise."""
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo))  # default WorkerPolicy(): no mcp_servers
    tid = await engine.prompt("w1", "do the thing")
    await terminal_turn(registry, tid)
    assert not any(e["event"] == "turn_mcp_diagnostics" for e in events.read("w1"))


# --- ECA-145: Engine._check_policy_hook_gap, exercised through a real _run_turn --
#
# `make_engine`'s fake `query` never dispatches a hook at all (it just yields a
# scripted message list), so `hook_calls` stays (0, 0) for every one of these
# turns regardless of what tools were "used". That is why the two turn-level
# tests below either script zero non-AskUserQuestion tool uses, or deliberately
# assert the event it causes — this file cannot prove a real turn's hook
# actually fires (that needs a real CLI dispatching a real hook), only that the
# wiring in Engine._run_turn is correct. The REAL proof that a suppressed hook
# is caught end to end is
# test_live_gate.py::test_a_suppressed_policy_hook_is_detected_in_a_real_turn.
#
# The arithmetic itself — specifically, that an AskUserQuestion dispatch cannot
# mask a gap in a DIFFERENT tool's dispatch, which the fake-query turns above
# cannot exercise since they can't produce a controlled (total, askq) split —
# is tested directly below.


async def test_policy_hook_gap_fires_when_a_tool_ran_with_no_hook_dispatch(
    make_engine, registry, repo, events
):
    """A scripted tool use with zero matching hook dispatch IS a gap — this is the
    fake harness's own blind spot (see module note above) standing in for the real
    regression: the arithmetic must still catch it and the turn must still finish
    normally (WARN, not FAIL — AC#3)."""
    engine, calls = make_engine([[a("Bash"), r("s1")]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "do the thing")
    turn = await terminal_turn(registry, tid)
    assert turn["state"] == "done", "a detected gap must not fail the turn"

    gaps = [e for e in events.read("w1") if e["event"] == "policy_hook_gap"]
    assert len(gaps) == 1
    assert gaps[0]["tool_uses"] == 1
    assert gaps[0]["hook_invocations"] == 0


async def test_policy_hook_gap_excludes_ask_user_question(make_engine, registry, repo, events):
    """AskUserQuestion is deliberately excluded from `required` — its own bridge
    owns it, so a turn that only ever used it must not read as a policy-hook gap
    even though (like every fake-query test) hook_calls is 0."""
    engine, calls = make_engine([[a("AskUserQuestion"), r("s1")]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "do the thing")
    await terminal_turn(registry, tid)
    assert not any(e["event"] == "policy_hook_gap" for e in events.read("w1"))


async def test_policy_hook_gap_an_ask_user_question_dispatch_cannot_mask_a_bash_gap(
    cfg, registry, events
):
    """Review-round regression test: a flat `total dispatches >= required` compare
    (the first cut of this check) let each AskUserQuestion dispatch forgive one
    un-dispatched call to a DIFFERENT tool, since both simply incremented the same
    counter. A turn that asked one question (hook fired once, correctly) and ran
    one Bash command with the hook suppressed must still be flagged — `hook_calls`
    of `(1, 1)` (one total dispatch, and it WAS the AskUserQuestion one) leaves
    zero real coverage for the Bash call.

    Calls `_check_policy_hook_gap` directly because `make_engine`'s fake `query`
    cannot produce a controlled split between "total" and "AskUserQuestion"
    dispatches — see the module note above.
    """
    bridge = QuestionBridge(registry, events)
    engine = Engine(cfg, registry, events, bridge)
    outcome = TurnOutcome(tools=["AskUserQuestion", "Bash"])

    engine._check_policy_hook_gap("w1", 1, outcome, (1, 1))
    gaps = [e for e in events.read("w1") if e["event"] == "policy_hook_gap"]
    assert gaps, "the AskUserQuestion dispatch masked the missing Bash dispatch"
    assert gaps[0]["tool_uses"] == 1
    assert gaps[0]["hook_invocations"] == 0

    # Both tools actually covered (a different worker key, so a fresh event log):
    # no gap.
    engine._check_policy_hook_gap("w2", 1, outcome, (2, 1))
    assert not any(e["event"] == "policy_hook_gap" for e in events.read("w2"))


async def test_mcp_startup_grace_delays_prompt_only_when_servers_granted(
    cfg, registry, events, monkeypatch, repo
):
    """ECA-101 AC3: query()'s own pre-first-message wait only ever covers
    'sdk'-type (in-process) mcp_servers, never the stdio/http/https servers a
    worker policy actually grants — so without a deliberate grace period, those
    servers get zero guaranteed head start against the model's first action."""
    grace_cfg = dataclasses.replace(cfg, mcp_startup_grace_s=0.2)
    calls: list[Any] = []
    monkeypatch.setattr(
        "worker_supervisor.engine.query", make_fake_query([[r("s1")]], calls)
    )
    monkeypatch.setattr(Engine, "_transcript_exists", lambda self, cwd, sid: True)
    bridge = QuestionBridge(registry, events)
    engine = Engine(grace_cfg, registry, events, bridge)
    try:
        await engine.spawn(
            "w1", str(repo), WorkerPolicy(mcp_servers={"context7": {"type": "stdio"}})
        )
        start = asyncio.get_event_loop().time()
        tid = await engine.prompt("w1", "do the thing")
        await terminal_turn(registry, tid)
        assert asyncio.get_event_loop().time() - start >= 0.2
    finally:
        await engine.stop()


async def test_mcp_startup_grace_skipped_without_mcp_grant(
    cfg, registry, events, monkeypatch, repo
):
    """The grace period must not tax lanes that were never granted any MCP server."""
    grace_cfg = dataclasses.replace(cfg, mcp_startup_grace_s=0.2)
    calls: list[Any] = []
    monkeypatch.setattr(
        "worker_supervisor.engine.query", make_fake_query([[r("s1")]], calls)
    )
    monkeypatch.setattr(Engine, "_transcript_exists", lambda self, cwd, sid: True)
    bridge = QuestionBridge(registry, events)
    engine = Engine(grace_cfg, registry, events, bridge)
    try:
        await engine.spawn("w1", str(repo))  # no mcp_servers granted
        start = asyncio.get_event_loop().time()
        tid = await engine.prompt("w1", "do the thing")
        await terminal_turn(registry, tid)
        assert asyncio.get_event_loop().time() - start < 0.2
    finally:
        await engine.stop()


# --- ECA-135: granted MCP credentials must never enter the child's argv ---------
#
# Confirmed live on mbpm2 (SDK 0.2.91 = bundled CLI 2.1.165; the original note said
# 2.1.220, which was PATH `claude`, not the spawned binary — corrected under ECA-138)
# on 2026-07-28, before the fix: a
# worker turn read both of its own planted sentinels out of its parent's argv with
# one `ps`, and `workers get` handed them back to the caller. These tests exercise
# the real SDK argv builder, so they fail against the pre-fix engine rather than
# merely restating what the new code does.

ECA135_SENTINEL = "ECA135-SENTINEL-vd83ka-not-a-real-credential"


def granted_policy() -> WorkerPolicy:
    return WorkerPolicy(
        mcp_servers={
            "paid": {
                "type": "http",
                "url": "http://127.0.0.1:1/mcp",
                "headers": {"Authorization": f"Bearer {ECA135_SENTINEL}"},
            }
        }
    )


def capture_config_state(monkeypatch, sink: dict) -> None:
    """Snapshot the credential file as the CLI sees it — DURING the turn.

    _worker_loop unlinks it the moment the turn ends (that transience is the point,
    and its own test), so anything asserted about the file itself has to be captured
    while it is live rather than read back afterwards."""
    original = Engine._write_mcp_config

    def spy(self, worker, turn_id, servers):
        arg = original(self, worker, turn_id, servers)
        if isinstance(arg, str):
            path = Path(arg)
            sink[worker] = {
                "path": path,
                "content": path.read_text(),
                "mode": stat.S_IMODE(path.stat().st_mode),
                "dir_mode": stat.S_IMODE(path.parent.stat().st_mode),
            }
        return arg

    monkeypatch.setattr(Engine, "_write_mcp_config", spy)


def sdk_argv(options: Any) -> list[str]:
    """The argv the REAL SDK would exec for these options, spawning nothing."""
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    resolved = dataclasses.replace(options, cli_path="/nonexistent/claude")
    return SubprocessCLITransport(prompt="x", options=resolved)._build_command()


async def test_granted_mcp_credentials_never_reach_the_cli_argv(
    make_engine, registry, repo
):
    """The load-bearing assertion: the secret is absent from the command line the
    SDK would exec, and what replaces it is the config PATH."""
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    argv = sdk_argv(calls[0])
    assert ECA135_SENTINEL not in " ".join(argv)
    assert argv[argv.index("--mcp-config") + 1] == calls[0].mcp_servers
    assert isinstance(calls[0].mcp_servers, str)


async def test_mcp_config_file_carries_the_policy_at_0600_in_a_0700_dir(
    make_engine, registry, repo, monkeypatch
):
    """Moving the credential from argv to disk is only a gain if the file is not
    itself loosely permissioned — and the content must still be what the CLI expects."""
    seen: dict = {}
    capture_config_state(monkeypatch, seen)
    engine, calls = make_engine([[r("s1")]])
    policy = granted_policy()
    await engine.spawn("w1", str(repo), policy)
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    live = seen["w1"]
    assert live["path"] == Path(calls[0].mcp_servers)
    assert json.loads(live["content"]) == {"mcpServers": policy.mcp_servers}
    assert live["mode"] == 0o600
    # NB: on a fresh dir under a tight umask (this dev host runs 077) mkdir alone
    # already yields 0700, so this line does not by itself prove the explicit
    # chmod is there — test_mcp_config_write_tightens_a_pre_existing_loose_file
    # is the one that pins it umask-independently. Kept as the end-state assertion.
    assert live["dir_mode"] == 0o700


async def test_a_pre_existing_loose_config_dir_is_tightened(
    make_engine, registry, repo, cfg, monkeypatch
):
    """mkdir's mode is umask-masked and skipped entirely when the directory already
    exists, so a dir left at 0755 by anything else must still be tightened. This is the
    assertion that pins the explicit chmod umask-independently — on a fresh dir under a
    tight umask (this dev host runs 077) mkdir alone already yields 0700."""
    cfg.mcp_config_dir.mkdir(parents=True)
    cfg.mcp_config_dir.chmod(0o755)

    seen: dict = {}
    capture_config_state(monkeypatch, seen)
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert seen["w1"]["dir_mode"] == 0o700
    assert seen["w1"]["mode"] == 0o600
    assert ECA135_SENTINEL in seen["w1"]["content"]


async def test_the_config_mode_does_not_depend_on_the_open_mode(
    make_engine, registry, repo, monkeypatch
):
    """O_EXCL means the file is always brand-new, so O_CREAT's 0600 normally governs and
    the explicit fchmod is belt-and-braces — for a filesystem that applies inherited
    ACLs or otherwise ignores the open mode. Simulate one, so the fchmod is pinned by
    something rather than carried on faith."""
    real_open, real_umask = os.open, os.umask
    monkeypatch.setattr(
        "worker_supervisor.engine.os.open",
        lambda path, flags, mode=0o777: real_open(path, flags, 0o666),
    )
    # Without this the host umask (077 here) masks the simulated loose mode back down
    # to 0600 on its own and the assertion below passes with no fchmod at all.
    old_umask = os.umask(0)
    monkeypatch.setattr(os, "umask", lambda m: old_umask)  # keep pytest teardown honest
    seen: dict = {}
    capture_config_state(monkeypatch, seen)
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    try:
        await terminal_turn(registry, await engine.prompt("w1", "go"))
    finally:
        real_umask(old_umask)

    assert seen["w1"]["mode"] == 0o600


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
async def test_a_pre_planted_link_never_receives_the_credential(
    make_engine, registry, repo, cfg, tmp_path, monkeypatch, link
):
    """The round-1 fix used a derived (therefore predictable) filename, and O_NOFOLLOW
    rejects symlinks while saying nothing about HARD links — a hard link planted at that
    path took the whole credential write: O_TRUNC destroying the target, fchmod setting
    it 0600, and the credential surviving at the attacker's path even after the turn-end
    unlink, which removes the link and not the inode.

    Two changes kill the class outright rather than by enumerating link types: the
    filename carries a random component (so it cannot be predicted), and the open is
    O_EXCL after a purge (so the daemon never opens an inode it did not just create).
    Whatever was planted is unlinked as a leftover, which for a link removes the LINK,
    never the victim."""
    victim = tmp_path / "victim.json"
    victim.write_text('{"operator": "config"}')
    victim.chmod(0o644)
    cfg.mcp_config_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "worker_supervisor.engine.secrets.token_hex", lambda n: "deadbeefdeadbeef"
    )
    # The turn id is assigned by the registry, so plant across the plausible range.
    for tid in range(1, 6):
        planted = cfg.mcp_config_dir / f"w1-{tid}-deadbeefdeadbeef.json"
        if link == "symlink":
            planted.symlink_to(victim)
        else:
            os.link(victim, planted)

    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    turn = await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert turn["state"] == "done"
    assert victim.read_text() == '{"operator": "config"}', "the victim was written!"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644, "the victim was chmod'd!"
    assert ECA135_SENTINEL not in victim.read_text()


async def test_lane_without_an_mcp_grant_is_byte_for_byte_unchanged(
    make_engine, registry, repo, cfg
):
    """Negative control: the un-granted lane keeps the empty-dict option and gets no
    --mcp-config at all, so this fix cannot regress the lanes it does not concern."""
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo))  # no mcp_servers granted
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert calls[0].mcp_servers == {}
    assert "--mcp-config" not in sdk_argv(calls[0])
    assert not cfg.mcp_config_dir.exists()


async def test_retry_ladder_writes_the_config_once(
    make_engine, registry, repo, monkeypatch
):
    """Written once per TURN, before the attempt loop. The path alone cannot show
    this — it is derived from the worker name, so a per-attempt write would land on
    the same path — so count the writes."""
    writes: list[str] = []
    original = Engine._write_mcp_config

    def counting(self, worker, turn_id, servers):
        writes.append(worker)
        return original(self, worker, turn_id, servers)

    monkeypatch.setattr(Engine, "_write_mcp_config", counting)

    engine, calls = make_engine([RuntimeError("boom 1"), [r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert len(calls) == 2, "expected the retry ladder to run two attempts"
    assert writes == ["w1"]
    assert isinstance(calls[0].mcp_servers, str)
    assert calls[0].mcp_servers == calls[1].mcp_servers


async def test_remove_sweeps_a_config_file_left_by_a_crashed_daemon(
    make_engine, registry, repo, cfg
):
    """With the turn-end unlink in place, `remove` covers exactly one case: a daemon
    that died mid-turn and left the file behind. Stage that state directly — asserting
    it after a NORMAL turn would assert nothing, since the file is already gone."""
    engine, _ = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    orphan = cfg.mcp_config_dir / "w1-99-deadbeefdeadbeef.json"
    assert not list(cfg.mcp_config_dir.iterdir()), "the turn-end purge should have run"
    orphan.write_text('{"mcpServers": {}}')  # simulate the crash leftover

    await engine.kill("w1")
    await engine.remove("w1")
    assert not orphan.exists()


# --- ECA-135, second round: the gaps the adversarial review found ---------------


async def test_strict_mcp_config_still_rides_with_the_grant(
    make_engine, registry, repo
):
    """The flag that stops an ambient repo .mcp.json widening a lane's surface sits one
    line from the code this change rewrote, and NOTHING in the suite pinned it —
    deleting it outright left 67/67 green. It matters more with a file config, not
    less: a corrupt file plus strict mode means a granted lane silently runs with zero
    servers rather than erroring."""
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert calls[0].strict_mcp_config is True
    assert "--strict-mcp-config" in sdk_argv(calls[0])


async def test_two_lanes_get_separate_files_and_remove_is_surgical(
    make_engine, registry, repo, cfg, monkeypatch
):
    """Cross-lane isolation of the FILES was pinned only incidentally, by another test
    hardcoding 'w1.json' — and cross-lane is the whole subject of ECA-135.

    Note what this does NOT claim: the files are 0600 but every lane runs as the same
    uid, so lane B can still read lane A's file. This asserts that the daemon keeps
    them SEPARATE and that removing one leaves the other, not that the OS keeps them
    apart. It does not."""
    seen: dict = {}
    capture_config_state(monkeypatch, seen)
    engine, calls = make_engine([[r("s1")], [r("s2")]])
    a_policy = granted_policy()  # carries ECA135_SENTINEL
    b_policy = WorkerPolicy(
        mcp_servers={"other": {"type": "stdio", "command": "/bin/true"}}
    )
    await engine.spawn("lanea", str(repo), a_policy)
    await engine.spawn("laneb", str(repo), b_policy)
    await terminal_turn(registry, await engine.prompt("lanea", "go"))
    await terminal_turn(registry, await engine.prompt("laneb", "go"))

    # CONTENT, not just paths: comparing filenames cannot see the failure that
    # actually matters here — lane B's file holding lane A's bearer.
    assert ECA135_SENTINEL in seen["lanea"]["content"]
    assert ECA135_SENTINEL not in seen["laneb"]["content"]
    assert json.loads(seen["laneb"]["content"]) == {"mcpServers": b_policy.mcp_servers}
    assert seen["lanea"]["path"] != seen["laneb"]["path"]
    assert not seen["lanea"]["path"].exists() and not seen["laneb"]["path"].exists()

    await engine.kill("lanea")
    await engine.remove("lanea")
    assert await registry.get_worker("laneb") is not None


async def test_config_file_does_not_outlive_its_turn(make_engine, registry, repo, cfg):
    """Argv's one virtue was being transient. A file that persisted past the turn — let
    alone past `kill` — would trade a turn-scoped exposure for a permanent one."""
    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert not Path(calls[0].mcp_servers).exists()
    assert list(cfg.mcp_config_dir.iterdir()) == []


async def test_mcp_config_write_failure_fails_the_turn_instead_of_wedging_the_lane(
    make_engine, registry, repo, cfg, events
):
    """Regression found in review: the write is the first filesystem touch _run_turn
    makes, and it sits OUTSIDE the attempt loop's handlers. Nothing supervises a runner
    task, so an escaping OSError killed the lane's loop — turn stuck `claimed`, worker
    stuck `running`, no event, no capsule, forever. It must fail the TURN."""
    cfg.mcp_config_dir.parent.mkdir(parents=True, exist_ok=True)
    cfg.mcp_config_dir.write_text("not a directory")  # mkdir(exist_ok=True) -> OSError

    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    turn = await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert turn["state"] == "error"
    assert "mcp config write failed" in turn["error"]
    assert calls == [], "the CLI must never have been spawned"
    assert (await registry.get_worker("w1"))["status"] == "idle"
    assert [e for e in events.read("w1") if e["event"] == "turn_error"]
    assert await capsule_paths(cfg, "w1"), "failure capsule missing"
    # NB: this is a cheap smoke check, NOT the guard against the wedge — terminal_turn
    # returns before the loop's finally, so it can pass with the runner already dying.
    # test_a_failing_turn_end_purge_cannot_kill_the_worker_loop is the one that bites
    # (verified by mutation); an earlier version of this comment claimed otherwise.
    assert not engine._runners["w1"].done(), "the runner loop died"


@pytest.mark.parametrize(
    "bad",
    ["../../.claude", "a/b", ".hidden", "..", "", "x" * 65, "na me", "w1\n", "w1\nx"],
)
async def test_spawn_rejects_names_that_are_not_safe_filenames(
    make_engine, repo, bad
):
    """A worker name becomes a filename, and since this change that filename is
    TRUNCATED and UNLINKED. Review demonstrated `../../.claude` overwriting the
    operator's real ~/.claude.json — the file the deploy generator reads live
    credentials out of."""
    engine, _ = make_engine([[r("s1")]])
    with pytest.raises(ValueError, match="invalid worker name"):
        await engine.spawn(bad, str(repo), granted_policy())


async def test_spawn_rejects_a_case_folded_collision(make_engine, repo):
    """APFS is case-insensitive; the registry's TEXT PRIMARY KEY is not. Two distinct
    workers would share one credential file and the last writer would win — a lane
    starting with another lane's bearers, which is exactly the leak this task is
    about."""
    engine, _ = make_engine([[r("s1")]])
    await engine.spawn("Ultra1", str(repo), granted_policy())
    with pytest.raises(ValueError, match="collides case-insensitively"):
        await engine.spawn("ultra1", str(repo), granted_policy())


async def test_a_symlinked_config_DIRECTORY_is_refused_not_followed(
    make_engine, registry, repo, cfg, tmp_path
):
    """Path.mkdir(exist_ok=True) accepts a symlink-to-directory and Path.chmod then
    chmods the TARGET — so without the explicit is_symlink guard the daemon would
    relocate a lane's credentials into an attacker-chosen directory and set it 0700."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o755)
    # A victim that MATCHES the purge glob. Without it the target is empty, so a purge
    # that follows the symlink and a purge that refuses look identical — which is how
    # the first version of this test missed a confused-deputy unlink.
    victim = elsewhere / "w1-1-deadbeefdeadbeef.json"
    victim.write_text('{"operator": "config"}')
    cfg.mcp_config_dir.parent.mkdir(parents=True, exist_ok=True)
    cfg.mcp_config_dir.symlink_to(elsewhere)

    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    turn = await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert turn["state"] == "error"
    assert victim.read_text() == '{"operator": "config"}', "the victim was deleted!"
    assert list(elsewhere.iterdir()) == [victim], "credentials landed in the target"
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755, "target was chmod'd"
    assert calls == []


# --- ECA-135 round 3: guards the round-2 tests could not see --------------------


async def test_a_failing_turn_end_purge_cannot_kill_the_worker_loop(
    make_engine, registry, repo, cfg, events, monkeypatch
):
    """The purge runs in `_worker_loop`'s finally, BEFORE `_after_turn`. An escape there
    kills the runner and silently stops all lifecycle chaining — no epoch roll, no
    restore enqueue, no auto-cycle — while the turn still reports success. That is the
    same wedge this task fixed at the write site and then reintroduced six lines above
    it; both reviewers found it independently.

    Make the purge genuinely fail: strip write permission from the config dir once the
    file is in place, so the unlink gets EACCES."""
    original = Engine._write_mcp_config

    def write_then_lock_the_dir(self, worker, turn_id, servers):
        arg = original(self, worker, turn_id, servers)
        self._cfg.mcp_config_dir.chmod(0o500)  # r-x: unlink now raises EACCES
        return arg

    monkeypatch.setattr(Engine, "_write_mcp_config", write_then_lock_the_dir)

    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    turn = await terminal_turn(registry, await engine.prompt("w1", "go"))
    try:
        assert turn["state"] == "done"

        # WAIT for the purge to have happened. `terminal_turn` returns as soon as the
        # turn row is terminal, which _run_turn writes BEFORE the loop's finally — so
        # asserting on the runner here would be a race, and would pass whether or not
        # the exception escapes. Waiting on the event is the deterministic form: if the
        # handler is gone, the exception escapes before the emit and this times out.
        async def _purge_failed():
            hits = [
                e for e in events.read("w1") if e["event"] == "mcp_config_purge_failed"
            ]
            return hits or None

        failures = await wait_until(_purge_failed)
        assert "PermissionError" in failures[0]["error"]
        assert not engine._runners["w1"].done(), "the runner loop died on cleanup"
    finally:
        cfg.mcp_config_dir.chmod(0o700)


async def test_a_link_planted_after_the_purge_is_still_refused(
    make_engine, registry, repo, cfg, tmp_path, monkeypatch
):
    """O_EXCL is the last line of defence, and the purge normally clears the path before
    it — so nothing pins O_EXCL unless the plant lands in the window BETWEEN them. Use
    the daemon's own `chmod` on the config dir as that seam: it runs after the purge and
    before the open."""
    victim = tmp_path / "victim.json"
    victim.write_text('{"operator": "config"}')
    monkeypatch.setattr(
        "worker_supervisor.engine.secrets.token_hex", lambda n: "deadbeefdeadbeef"
    )
    real_chmod = Path.chmod

    def chmod_then_plant(self, mode, **kw):
        real_chmod(self, mode, **kw)
        if self == cfg.mcp_config_dir:
            for tid in range(1, 6):
                target = cfg.mcp_config_dir / f"w1-{tid}-deadbeefdeadbeef.json"
                if not target.exists():
                    os.link(victim, target)

    monkeypatch.setattr(Path, "chmod", chmod_then_plant)

    engine, calls = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    turn = await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert turn["state"] == "error"
    assert "File exists" in turn["error"] or "EEXIST" in turn["error"]
    assert victim.read_text() == '{"operator": "config"}', "the victim was written!"
    assert calls == [], "the CLI must never have been spawned"


async def test_the_config_filename_is_not_predictable(
    make_engine, registry, repo, monkeypatch
):
    """Unpredictability is what makes pre-planting impossible, so it is a security
    property and not a detail: a name derived from the worker alone is guessable from
    `workers status` or the mesh announcement."""
    paths: list[Path] = []
    original = Engine._write_mcp_config

    def record(self, worker, turn_id, servers):
        arg = original(self, worker, turn_id, servers)
        paths.append(Path(arg))
        return arg

    monkeypatch.setattr(Engine, "_write_mcp_config", record)
    engine, calls = make_engine([[r("s1")], [r("s2")]])
    await engine.spawn("w1", str(repo), granted_policy())
    await terminal_turn(registry, await engine.prompt("w1", "one"))
    await terminal_turn(registry, await engine.prompt("w1", "two"))

    assert len(paths) == 2
    tokens = [p.stem.rsplit("-", 1)[-1] for p in paths]
    assert tokens[0] != tokens[1], "the filename repeats across turns"
    assert all(len(t) == 16 and set(t) <= set("0123456789abcdef") for t in tokens)


async def test_boot_sweeps_credential_files_a_killed_daemon_left_behind(
    make_engine, registry, repo, cfg
):
    """A SIGKILLed daemon runs no `finally`, and an orphan for a lane never prompted
    again would sit there indefinitely — the permanent exposure this fix exists to
    avoid. No turn can be in flight at boot, so everything present is an orphan."""
    engine, _ = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo), granted_policy())
    cfg.mcp_config_dir.mkdir(parents=True, exist_ok=True)
    orphan = cfg.mcp_config_dir / "w1-7-deadbeefdeadbeef.json"
    orphan.write_text('{"mcpServers": {"paid": {"headers": {"x": "leftover"}}}}')

    await engine.start()

    assert not orphan.exists()


async def test_purging_a_hostile_persisted_name_cannot_escape_the_directory(
    make_engine, repo, tmp_path, events
):
    """Spawn-time validation does not cover a row persisted BEFORE that validator
    existed, and `remove`, the turn-end purge and boot recovery all consume a stored
    name as a path. Hence the guard sits at path derivation, where every consumer passes
    through — verified here by calling the purge directly with a name `spawn` would now
    reject."""
    victim = tmp_path / "outside.json"
    victim.write_text("operator data")

    engine, _ = make_engine([[r("s1")]])
    before = set(tmp_path.iterdir())
    engine._purge_mcp_config("../../outside")  # must not raise, must not delete

    assert victim.exists(), "the purge escaped the config directory"
    refused = [e for e in events.read("-") if e["event"] == "mcp_config_purge_refused"]
    assert refused and refused[-1]["lane"] == "../../outside"
    # ...and the REFUSAL must not itself escape: EventLog names its file after the key
    # it is given, so emitting under the hostile name would write outside the home.
    assert set(tmp_path.iterdir()) == before, "the refusal event escaped the directory"
    # ECA-137: pins that this caller passes the DAEMON KEY deliberately. Since EventLog
    # now re-keys a refused key itself, emitting under `worker` here would land in the
    # same file and satisfy every assertion above — mutation-testing found exactly that
    # mutant surviving. An absent `log_key_refused` is what distinguishes the two.
    assert "log_key_refused" not in refused[-1]


# --- ECA-135 round 4: findings the round-3 suite passed straight through --------


async def test_a_hostile_persisted_name_cannot_plant_a_config_outside_the_home(
    make_engine, registry, repo, tmp_path, cfg
):
    """Round 2 fixed the traversal UNLINK; this is the traversal WRITE, which replaced
    it. `_write_mcp_config` derived its path directly, and the purge on the line above
    validates but SWALLOWS the refusal by design — so it could never be what protects
    this path. A row persisted before spawn-time validation existed would plant a
    credential file outside the supervisor home, where neither the purge nor the boot
    sweep can ever reach it."""
    hostile = "../../escaped"
    await registry.spawn_worker(  # bypasses Engine.spawn, as a legacy row does
        hostile, str(repo), json.loads(granted_policy().to_json())
    )
    cfg.mcp_config_dir.mkdir(parents=True, exist_ok=True)

    engine, calls = make_engine([[r("s1")]])
    engine._ensure_runner(hostile)
    turn = await terminal_turn(registry, await engine.prompt(hostile, "go"))

    assert turn["state"] == "error"
    assert "invalid worker name" in turn["error"]
    assert calls == [], "the CLI must never have been spawned"

    # No CREDENTIAL-bearing file anywhere outside the config dir. Asserted on content
    # rather than on filenames because when this test was written the same error path
    # DID write a failure capsule through the hostile name — a separate pre-existing
    # traversal in capsule.py/events.py, which a filename-based assertion here would
    # have failed on without being this defect. ECA-137 has since closed that one, so
    # nothing escapes on this path any more (proved by its own test below); the
    # content-based assertion is kept because it is the narrower claim, and it is what
    # this test is actually about.
    outside = cfg.mcp_config_dir.parent.parent
    leaked = [
        f for f in outside.rglob("*.json")
        if cfg.mcp_config_dir not in f.parents and ECA135_SENTINEL in f.read_text()
    ]
    assert leaked == [], f"a credential was planted outside the config dir: {leaked}"


async def test_purging_one_lane_leaves_a_prefix_named_lanes_config_alone(
    make_engine, registry, repo, cfg
):
    """'-' is the field separator AND a legal name character, so a bare `<worker>-*.json`
    glob also matches a lane whose name EXTENDS this one. Purging `ultra` would delete
    `ultra-2`'s live in-flight config, and under strict_mcp_config that lane then runs
    with zero MCP servers and no error at all — a cross-lane break introduced by the
    cleanup that exists to prevent cross-lane leakage."""
    engine, _ = make_engine([[r("s1")]])
    cfg.mcp_config_dir.mkdir(parents=True, exist_ok=True)
    mine = cfg.mcp_config_dir / "ultra-1-aaaaaaaaaaaaaaaa.json"
    theirs = cfg.mcp_config_dir / "ultra-2-1-bbbbbbbbbbbbbbbb.json"
    mine.write_text("{}")
    theirs.write_text("{}")

    engine._purge_mcp_config("ultra")

    assert not mine.exists()
    assert theirs.exists(), "purging 'ultra' deleted lane 'ultra-2's live config"


async def test_the_purge_survives_a_failing_event_emit(
    make_engine, registry, repo, cfg, monkeypatch
):
    """`emit` is itself an unguarded file open, so an emit from INSIDE a handler can
    escape and re-create the wedge this method exists to prevent — the docstring's
    'NEVER raises' is the load-bearing part of the contract."""
    engine, _ = make_engine([[r("s1")]])
    cfg.mcp_config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        engine._events, "emit", lambda *a, **k: (_ for _ in ()).throw(OSError("logs gone"))
    )

    engine._purge_mcp_config("../../hostile")  # refusal path -> emit raises
    engine._purge_mcp_config("w1")  # ordinary path


def test_boot_refuses_a_second_daemon_before_touching_the_config_dir():
    """The sweep deletes every file in the MCP config dir on the premise that no turn can
    be in flight at boot — a premise the socket preflight is what actually enforces. With
    the sweep first, a mistakenly-started second instance would delete the LIVE daemon's
    in-flight credential files before discovering it should not have booted (the
    2026-07-07 double-daemon incident, now with a destructive action ahead of the check).

    Scope of what the ordering buys: the preflight keys on the SOCKET, so two daemons
    sharing a HOME with different SUPERVISOR_SOCKET overrides both pass it. See
    _sweep_orphan_mcp_configs — the premise is one daemon per HOME, and this check only
    delivers one per socket.
    """
    import worker_supervisor.__main__ as daemon_main

    source = Path(daemon_main.__file__).read_text()  # not a CWD-relative path
    assert source.index("preflight_socket_check") < source.index("await engine.start()")


async def test_a_broken_config_dir_is_reported_not_silently_skipped(
    make_engine, cfg, events
):
    """`Path.glob` swallows ENOTDIR and returns nothing, so a config dir that exists as a
    FILE looked like 'nothing to purge' and emitted no diagnostic at all. A granted lane
    still surfaces it through its turn error; an un-granted lane gave zero signal."""
    engine, _ = make_engine([[r("s1")]])
    cfg.mcp_config_dir.parent.mkdir(parents=True, exist_ok=True)
    cfg.mcp_config_dir.write_text("not a directory")

    engine._purge_mcp_config("w1")  # must not raise

    refused = [e for e in events.read("-") if e["event"] == "mcp_config_purge_refused"]
    assert refused and "not a directory" in refused[-1]["error"]


# --- ECA-137: the two writers ECA-135 left, closed at path derivation ------------


async def test_a_hostile_persisted_name_cannot_write_a_log_or_capsule_outside_the_home(
    make_engine, registry, repo, tmp_path, cfg, events
):
    """The escape this task was filed for, end to end.

    ECA-135 validated `Engine.spawn` and the MCP-config path, so no NEW row can carry a
    hostile name — but `EventLog` and `write_capsule` still derived filenames from a
    PERSISTED one, and a row written before that validator existed still reaches them.
    Demonstrated at the time: an ECA-135 test produced a capsule at
    `<home>/../../escaped-turn1-<ts>.json`.

    Driven with an UNGRANTED policy on purpose. A granted lane fails inside
    `_write_mcp_config`, whose ECA-135 guard would refuse the name before either of
    these writers ran — so it could never exercise them. Ungranted, `_write_mcp_config`
    returns `{}` before validating, the turn runs the full failure ladder, and every
    event plus the failure capsule is written through the hostile name.

    Asserts on the tree OUTSIDE the home rather than on an exception, because the defect
    was a file appearing where it should not.
    """
    hostile = "../../escaped"
    await registry.spawn_worker(  # bypasses Engine.spawn, as a legacy row does
        hostile, str(repo), json.loads(WorkerPolicy().to_json())  # ungranted — see above
    )

    outside_before = {p for p in tmp_path.rglob("*") if cfg.home not in p.parents}
    engine, _ = make_engine([RuntimeError("died"), RuntimeError("died again")])
    engine._ensure_runner(hostile)
    turn = await terminal_turn(registry, await engine.prompt(hostile, "go"))

    assert turn["state"] == "error"  # the ladder ran; the capsule path was reached
    outside_after = {p for p in tmp_path.rglob("*") if cfg.home not in p.parents}
    assert outside_after == outside_before, "a log or capsule escaped the home"
    assert not (tmp_path / "escaped.jsonl").exists()
    assert list(tmp_path.glob("escaped-turn*.json")) == []

    # Nothing was silently dropped: the lane's events were re-keyed to the daemon key,
    # carrying the hostile name in the body as evidence.
    refused = [e for e in events.read("-") if e.get("log_key_refused")]
    assert refused, "the hostile lane's events vanished instead of being re-keyed"
    assert {e["worker"] for e in refused} == {hostile}
    assert any(e["event"] == "failure_capsule_error" for e in refused)


async def test_a_legal_lane_still_gets_its_own_log_and_capsule(
    make_engine, registry, repo, cfg, events
):
    """AC#3 at the engine level: the guard must be invisible to a real lane, and the
    capsule must still land — a refusal that fired on well-formed names would make this
    task a regression rather than a fix."""
    engine, _ = make_engine([RuntimeError("died"), RuntimeError("died again")])
    await engine.spawn("ultra-2", str(repo))
    turn = await terminal_turn(registry, await engine.prompt("ultra-2", "go"))

    assert turn["state"] == "error"
    assert (cfg.logs_dir / "ultra-2.jsonl").exists()
    assert not any(e.get("log_key_refused") for e in events.read("ultra-2"))
    capsules = await capsule_paths(cfg, "ultra-2")
    assert len(capsules) == 1, f"expected one capsule, got {capsules}"


# -- ECA-148: daemon shutdown with a turn in flight -----------------------------


async def _turn_running(registry, turn_id: int):
    t = await registry.get_turn(turn_id)
    return t if t and t["state"] == "running" else None


async def test_stop_returns_with_a_turn_in_flight(make_engine, registry, repo, events):
    """ECA-148: `stop()` must not be wedged by the very turn it is cancelling.

    Bounded TWICE, and the two bounds catch different regressions.

    `wait_for` catches an unbounded `stop()`. It has to be here rather than left
    to pytest, because the `make_engine` fixture's teardown calls `stop()` too —
    so an unbounded `stop()` does not fail one test, it wedges the whole
    interpreter and the run only reports its (true) pass count when SIGINT
    releases it. That is ECA-146's symptom exactly.

    The `stop_incomplete` assertion catches the actual defect. Restore the
    swallow in `_worker_loop` and `stop()` still RETURNS — the grace expires —
    but it abandons a live runner, and only that event tells the two apart.
    """
    engine, _ = make_engine([[Hang()]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "work")
    await wait_until(lambda: _turn_running(registry, tid))

    await asyncio.wait_for(engine.stop(), timeout=STOP_GRACE_S * 3)

    abandoned = [e for e in events.read(DAEMON_KEY) if e["event"] == "stop_incomplete"]
    assert not abandoned, (
        f"stop() gave up on a runner instead of exiting cleanly: {abandoned} — the "
        "runner swallowed its own cancellation and went back round the loop"
    )
    assert not [t for t in engine._runners.values() if not t.done()]


async def test_a_turn_interrupted_by_stop_is_redelivered_on_the_next_boot(
    make_engine, registry, repo, events
):
    """ECA-148 AC#2: what happens to the turn `stop()` interrupted.

    It is left `running` ON PURPOSE and healed by `boot_reconcile`, which is the
    same at-least-once contract a crash already has — the work is redelivered,
    not silently dropped. Asserted by actually running the reconcile rather than
    by asserting the intent, and paired with the `turn_interrupted` event so the
    gap in `workers events` is explained rather than mysterious.
    """
    engine, _ = make_engine([[Hang()]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "work")
    await wait_until(lambda: _turn_running(registry, tid))

    await asyncio.wait_for(engine.stop(), timeout=STOP_GRACE_S * 3)

    interrupted = [e for e in events.read("w1") if e["event"] == "turn_interrupted"]
    assert [e["turn_id"] for e in interrupted] == [tid], events.read("w1")
    assert (await registry.get_turn(tid))["state"] == "running"

    stats = await registry.boot_reconcile()
    assert stats["turns_redelivered"] == 1, stats
    healed = await registry.get_turn(tid)
    assert healed["state"] == "queued"
    assert healed["redeliveries"] == 1


async def test_kill_still_leaves_the_runner_alive(make_engine, registry, repo):
    """The other half of the branch ECA-148 split, and the reason it was one branch.

    `kill()` cancels the TURN task and deliberately leaves the runner loop alive
    so it can re-read worker status; `stop()` cancels the RUNNER and needs it to
    exit. Both arrive at the same `await task` as a CancelledError with
    `task.cancelled()` true. Fixing the shutdown case by simply re-raising would
    have broken this one, so it is asserted alongside.
    """
    engine, _ = make_engine([[Hang()]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "work")
    await wait_until(lambda: _turn_running(registry, tid))
    runner = engine._runners["w1"]

    await engine.kill("w1")
    await asyncio.wait_for(asyncio.shield(runner), timeout=5)

    # It exited by reading `killed` off the worker row at the top of the loop —
    # not by having the shutdown path's re-raise torn through it.
    assert not runner.cancelled()
    assert (await registry.get_turn(tid))["state"] == "killed"


async def test_stop_gives_up_loudly_on_a_task_cancellation_cannot_reach(
    make_engine, repo, events, monkeypatch
):
    """The backstop bound, tested against the shape that motivates it.

    `stop()` has no cure for an await that cancellation cannot reach, and one
    demonstrably exists: the SDK's `Query.close()` runs inside
    `anyio.CancelScope(shield=True)` and its own docstring says it is NOT bounded
    (unlike `transport.close()`'s shield), so a wedged transport close holds the
    runner open no matter what `stop()` sends. Modelled with a task that swallows
    its cancellation, because a unit test cannot honestly wedge a real transport.

    What is asserted is therefore not "it recovers" — it does not — but that the
    daemon still finishes shutting down and NAMES what it abandoned, instead of
    waiting on it forever.

    Deliberately NOT written with `asyncio.wait_for(engine.stop(), ...)`, and the
    reason is this defect's own moral. Review round measured it: with only the
    bound reverted, `wait_for`'s timeout cancels `stop()`, that cancellation
    propagates into the task that ignores cancellation, and the test runs forever
    — "3 passed ... 240.08s", released by SIGINT. The test written to close
    ECA-146 reintroduced it. `asyncio.wait` never cancels what it waits on, and
    the wedged task is released BEFORE anything is asserted, so a regression here
    fails in bounded time instead of hanging.
    """
    monkeypatch.setattr("worker_supervisor.engine.STOP_GRACE_S", 0.2)
    engine, _ = make_engine([])
    release = asyncio.Event()

    async def unreachable() -> None:
        while not release.is_set():
            try:
                await asyncio.wait_for(release.wait(), timeout=3600)
            except asyncio.CancelledError:
                pass  # a shielded await: the cancel does not land

    stuck = asyncio.create_task(unreachable(), name="wedged-close")
    engine._watchdogs.add(stuck)
    stopper = asyncio.create_task(engine.stop(), name="stopper")
    done, _ = await asyncio.wait({stopper}, timeout=10)
    still_wedged = not stuck.done()

    release.set()  # unconditional, and BEFORE any assert
    await asyncio.wait_for(stuck, timeout=5)
    await asyncio.wait_for(stopper, timeout=5)

    assert stopper in done, "stop() waited past its own grace on an unreachable task"
    assert still_wedged, "the probe task was not actually unreachable"
    abandoned = [e for e in events.read(DAEMON_KEY) if e["event"] == "stop_incomplete"]
    assert [e["abandoned"] for e in abandoned] == [["wedged-close"]], abandoned


async def test_stop_in_the_window_just_after_a_turn_finishes(
    make_engine, registry, repo, events, monkeypatch
):
    """The window ECA-146 kept falling into, asserted directly (ECA-148 AC#4).

    `terminal_turn`/`wait_until` — the helpers nearly every test in this file ends
    on — observe the turn row the instant `finish_turn` commits, which is BEFORE
    `_worker_loop` returns from `await task`. So at the moment a typical test stops
    asserting, the runner is still parked on the in-flight turn task, and the
    `make_engine` teardown's `stop()` lands in that gap. Pre-fix that swallowed the
    cancellation and the INTERPRETER never exited: measured, the probe printed
    "1 passed in 45.09s" and only SIGINT released it — ECA-146's symptom exactly,
    with no SDK cleanup anywhere in the picture (the fake `query` never enters it).

    `set_worker_status` is slowed to WIDEN that window, not to create it: it is the
    last await in `_run_turn`, already after `finish_turn`. Un-widened, the gap is a
    scheduling accident — which is why ECA-146 was never reproducible on demand.

    Stopping EXPLICITLY here rather than leaving it to teardown is what keeps a
    regression a failure instead of a hang.
    """
    original = registry.set_worker_status

    async def slow(*a, **k):
        await asyncio.sleep(0.5)
        return await original(*a, **k)

    monkeypatch.setattr(registry, "set_worker_status", slow)
    engine, _ = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "work")
    assert (await terminal_turn(registry, tid))["state"] == "done"

    await asyncio.wait_for(engine.stop(), timeout=STOP_GRACE_S * 3)

    assert not [e for e in events.read(DAEMON_KEY) if e["event"] == "stop_incomplete"]
    # And it must not CLAIM an interruption here. Review round: the first version
    # emitted `turn_interrupted` unconditionally, so this very window — the widest
    # one — produced an event promising a redelivery for a turn that was already
    # `done`. Nothing is owed; nothing should be announced.
    assert not [e for e in events.read("w1") if e["event"] == "turn_interrupted"]


async def test_stop_lets_after_turn_finish_chaining_the_epoch(
    make_engine, registry, repo, monkeypatch
):
    """ECA-148 review round: `_after_turn` must not be torn in half by shutdown.

    A `cycle_handover` rolls the epoch and THEN enqueues the restore turn. Those
    are two awaits, and `boot_reconcile` cannot heal a cancel between them: it
    requeues claimed/running TURNS, and the lost restore was never a turn row.
    The lane would come back on a fresh epoch with an empty queue and no handover
    restore — silently amnesiac, and with no event saying so.

    The patched `roll_epoch` sleeps AFTER delegating, so the cancel lands in
    exactly that gap. Without the shield at the call site this leaves `seq == 2`
    and an empty queue; the queued `restore` is the discriminator.
    """
    original = registry.roll_epoch
    rolled = asyncio.Event()

    async def slow_roll(*a, **k):
        result = await original(*a, **k)
        rolled.set()
        await asyncio.sleep(0.5)
        return result

    monkeypatch.setattr(registry, "roll_epoch", slow_roll)
    engine, _ = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo))
    await engine.cycle("w1")
    await asyncio.wait_for(rolled.wait(), timeout=10)

    await asyncio.wait_for(engine.stop(), timeout=STOP_GRACE_S * 3)

    assert (await registry.current_epoch("w1"))["seq"] == 2
    queued = await registry.next_queued_turn("w1")
    assert queued is not None and queued["kind"] == "restore", (
        "the epoch rolled but the restore turn was never enqueued — the lane would "
        "come back cycled and ungrounded"
    )


async def test_the_transcript_watchdog_is_named(make_engine, registry, repo, monkeypatch):
    """`stop_incomplete.abandoned` exists to say WHAT shutdown gave up on.

    Review round: the transcript watchdogs were created unnamed, so half the
    population that event can name reported as "Task-23" — nothing, in the one
    field whose entire job is identification.
    """
    started, release = asyncio.Event(), asyncio.Event()

    async def blocking(self, *a, **k):
        started.set()
        await release.wait()

    monkeypatch.setattr(Engine, "_verify_transcript_persisted", blocking)
    engine, _ = make_engine([[r("s1")]])
    await engine.spawn("w1", str(repo))
    tid = await engine.prompt("w1", "work")
    await asyncio.wait_for(started.wait(), timeout=10)
    names = sorted(t.get_name() for t in engine._watchdogs)
    release.set()

    assert names == [f"transcript-watchdog-w1-turn{tid}"], names


# --- ECA-141: the other pass-through fields, wedge class closed at reload -------


async def test_a_persisted_row_with_every_pass_through_field_malformed_still_completes(
    make_engine, registry, repo
):
    """The wedge ECA-141 was filed for, driven end to end: a policy row with every
    uncoerced pass-through field wrong-shaped, exactly as a control-socket caller (or
    a row surviving from before this fix) could persist it.

    Pre-fix, `Engine._run_turn` crashed synchronously and unguarded while building
    `options_snapshot` — `policy.base_tools()` (allowed_tools), then
    `self._cfg.limits.override(policy.limits)`, then `policy.mcp_servers.keys()` —
    all BEFORE the attempt-loop's own try/except exists. `_worker_loop` only catches
    `asyncio.CancelledError` around the turn task, so any of those escaped silently:
    no `turn_retry` event, no `_fail_turn`, the turn stuck `claimed` and the worker
    stuck `running` forever. `allow_env`'s crash (`validate_allow_env`, not iterable)
    is the same class, slightly later, inside `ClaudeAgentOptions` construction.

    Bypasses `Engine.spawn` on purpose (`registry.spawn_worker` directly), the same
    way a row from before this fix — or written by any future caller that skips
    `WorkerPolicy.coerced()` — would reach `from_json` on reload. The claim under
    test is that `from_json` alone is sufficient: the turn must reach `done`, not
    merely avoid raising into an unrelated failure path.
    """
    hostile_policy = {
        "allowed_tools": None,
        "allow_env": 42,
        "guard_hooks": [],
        "limits": "not-a-dict",
        "mcp_servers": [1, 2, 3],
    }
    await registry.spawn_worker("w1", str(repo), hostile_policy)  # bypasses Engine.spawn

    engine, calls = make_engine([[r("s1")]])
    engine._ensure_runner("w1")
    turn = await terminal_turn(registry, await engine.prompt("w1", "go"))

    assert turn["state"] == "done", turn
    assert len(calls) == 1, "the malformed row must not have killed the runner loop pre-turn"
