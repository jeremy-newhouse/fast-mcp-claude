"""ECA-137: the two writers that derive a filename from a worker name — `EventLog`
and `write_capsule` — must refuse a name that is not a plain filename component.

Both were left unguarded by ECA-135 (which validated `Engine.spawn` and the MCP-config
path derivation) and by ECA-136 (which rewrote both functions for permissions and
O_EXCL without touching the name). The escape was demonstrated, not theorised: an
ECA-135 regression test persisted a row named `../../escaped` and a failure capsule
landed outside the supervisor home.

Each test asserts BOTH that nothing appeared outside the home and that the refusal
actually happened, because neither alone is sufficient and the two carry the weight for
different inputs. Falsifying this file (guards replaced with `if False`, `__pycache__`
purged) showed why: for a traversal shape like `../../escaped` it is the tree assertion
that fails, but for `with space` or `.hidden` nothing escapes at all — those would create
an odd file INSIDE the home — and only the refusal assertion catches them. A raises-only
test would likewise pass a guard that raised the right error after already creating the
file. Every case in this file fails with the guards removed, and the run FINISHES rather
than hanging — `follow` had to be given an explicit timeout to make that true.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from worker_supervisor.capsule import write_capsule
from worker_supervisor.events import EventLog
from worker_supervisor.names import (
    DAEMON_KEY,
    RESERVED_LOG_KEYS,
    SUPERVISOR_STREAM,
    is_safe_worker_name,
    require_safe_log_key,
    require_safe_worker_name,
)

# Names that must never reach a path. `..` alone and `../x` are the traversal shapes;
# the absolute path is the OTHER escape (`dir / "/etc/x"` discards `dir` entirely in
# pathlib, which no amount of `..`-checking would catch); the rest are shapes that
# produce a surprising filename rather than an escape.
HOSTILE = [
    "../../escaped",
    "..",
    "../x",
    "a/../../b",
    "/absolute",
    "sub/dir",
    ".hidden",
    "",
    "with space",
    "semi;colon",
    "star*glob",
    "nul\x00byte",
    "trailing\n",  # the `$`-vs-fullmatch trap ECA-135 documented
    "x" * 65,  # one past the 64-char ceiling
    # `a..b` cannot traverse on its own (no separator), so the regex would let it
    # through and nothing would escape. It is here because the refusal MESSAGE promises
    # "contain no '..'", and mutation-testing found that dropping the `..` check
    # otherwise survives: the only shapes covering it also carry a `/`, which the
    # charset already rejects. Pinning it keeps the promise honest and keeps the check
    # standing if the charset is ever loosened.
    "a..b",
]

# Real shapes in use on the live fleet (mini2 + mbpm2), plus the boundary cases.
LEGAL = ["w1", "ultra1", "ultra-2", "eca.brain", "A", "9", "x" * 64, "a_b-c.d"]


def _tree(root):
    """Every path under `root` that is NOT inside the supervisor home."""
    return {p for p in root.rglob("*") if "home" not in p.relative_to(root).parts[:1]}


# --- the predicates ------------------------------------------------------------


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_names_are_refused_by_both_predicates(name):
    assert not is_safe_worker_name(name)
    with pytest.raises(ValueError, match="invalid worker name"):
        require_safe_worker_name(name)
    with pytest.raises(ValueError, match="invalid event log key"):
        require_safe_log_key(name)


@pytest.mark.parametrize("name", LEGAL)
def test_legal_names_pass_both_predicates(name):
    assert is_safe_worker_name(name)
    require_safe_worker_name(name)
    require_safe_log_key(name)


# Not strings at all. `server.py` hands control-socket JSON straight through with no
# coercion (`args["name"]`), so these are reachable inputs, not hypotheticals.
NOT_STRINGS = [None, 123, 1.5, True, b"bytes", ["w1"], {"name": "w1"}, ()]


@pytest.mark.parametrize("value", NOT_STRINGS)
def test_a_non_string_key_is_refused_as_ValueError_not_TypeError(value):
    """Found while trying to break this guard rather than confirm it.

    Against a `str` pattern, `re.fullmatch(123)` raises TypeError — and TypeError is
    NOT what the callers here are written for: `EventLog.emit` catches ValueError
    specifically, so a non-str key would sail past its handler and re-create the exact
    lane wedge the re-keying exists to prevent. The mutation pass could not find this:
    every mutant it ran was still fed a string.
    """
    assert not is_safe_worker_name(value)
    with pytest.raises(ValueError):
        require_safe_worker_name(value)
    with pytest.raises(ValueError):
        require_safe_log_key(value)


@pytest.mark.parametrize("value", NOT_STRINGS)
def test_emit_holds_its_never_raises_contract_for_a_non_string_key(cfg, tmp_path, value):
    log = EventLog(cfg.logs_dir)
    before = _tree(tmp_path)

    record = log.emit(value, "turn_start")  # must not raise — that is the whole point

    assert record["log_key_refused"] is True
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("key", sorted(RESERVED_LOG_KEYS))
def test_a_reserved_key_is_a_log_key_but_never_a_worker_name(key):
    """The whole reason there are two predicates.

    Both reserved keys fail the worker-name pattern (which requires an alphanumeric first
    character), so guarding `EventLog` with the worker predicate — which is what this task
    was FILED asking for — would refuse the daemon's own streams: `-`, carrying ECA-135's
    `mcp_config_purge_refused` (the one event whose job is to report a refused name without
    writing through it), and `_supervisor`, carrying the live announce loop.

    Failing the worker pattern is also what makes them safe as reserved keys: no lane can
    be spawned under either name, so a reserved stream can never be a lane's log.
    """
    require_safe_log_key(key)  # must not raise
    assert not is_safe_worker_name(key)
    with pytest.raises(ValueError, match="invalid worker name"):
        require_safe_worker_name(key)


def test_every_daemon_owned_stream_key_in_the_source_is_reserved():
    """The regression this file did not catch until the LIVE logs were listed.

    `presence.py` emits three announce-loop events under `_supervisor`, a pseudo-worker
    key that fails the worker pattern. The first version of this guard admitted only `-`,
    so it silently refused all three, stopped appending to the `logs/_supervisor.jsonl`
    that exists on both supervisor hosts today, and would have made
    `workers events --name _supervisor` raise — with the entire suite green, because
    nothing exercised a presence emit.

    So this asserts the COUPLING rather than the behaviour of one emit: any daemon-owned
    stream constant must be in the reserved set. A future pseudo-stream added the same way
    fails here instead of on a live host.
    """
    from worker_supervisor import presence

    assert presence._SUPERVISOR_STREAM in RESERVED_LOG_KEYS
    assert presence._SUPERVISOR_STREAM == SUPERVISOR_STREAM, "presence re-declared the key"


def test_the_reserved_key_literals_match_what_is_on_disk_in_production():
    """These two strings ARE filenames on two production hosts.

    `logs/-.jsonl` and `logs/_supervisor.jsonl` exist on mini2 and mbpm2 today (48 live
    identifiers were checked against this module's predicates for ECA-137 AC#3). Renaming
    either constant silently orphans an existing stream and starts a new one beside it,
    which no other test here would notice — every one of them derives its expectation from
    the constant it is testing. Mutation-testing found exactly that hole.
    """
    assert DAEMON_KEY == "-"
    assert SUPERVISOR_STREAM == "_supervisor"
    assert RESERVED_LOG_KEYS == frozenset({"-", "_supervisor"})


def test_reserved_keys_round_trip_through_emit_and_read(cfg, tmp_path):
    log = EventLog(cfg.logs_dir)
    for key in sorted(RESERVED_LOG_KEYS):
        record = log.emit(key, "presence_beat", workers=3)
        assert "log_key_refused" not in record, f"{key} was refused"
        assert (cfg.logs_dir / f"{key}.jsonl").exists()
        assert log.read(key)[-1]["workers"] == 3
    assert _tree(tmp_path) == set()


# --- EventLog ------------------------------------------------------------------


@pytest.mark.parametrize("name", HOSTILE)
def test_emit_under_a_hostile_key_writes_nothing_outside_the_home(cfg, tmp_path, name):
    log = EventLog(cfg.logs_dir)
    before = _tree(tmp_path)

    record = log.emit(name, "turn_start", turn_id=1)

    assert _tree(tmp_path) == before, "emit escaped the supervisor home"
    # ...and it did not drop the record either: it lands under the daemon key, stamped.
    assert record["log_key_refused"] is True
    daemon_log = log.read(DAEMON_KEY)
    assert daemon_log[-1]["event"] == "turn_start"
    assert daemon_log[-1]["worker"] == name, "the rejected name must survive as evidence"
    assert daemon_log[-1]["log_key_refused"] is True
    assert not (cfg.logs_dir / f"{name}.jsonl").exists()


def test_emit_never_raises_for_a_hostile_key(cfg):
    """`emit` is called from 28 engine sites, several inside failure handlers. ECA-135
    established the cost of an escape from one of those: the worker's runner loop dies
    and the lane wedges silently while still accepting prompts. So this refusal is the
    one that must NOT propagate."""
    log = EventLog(cfg.logs_dir)
    assert log.emit("../../escaped", "turn_error", error="x")["log_key_refused"] is True


def test_emit_still_propagates_an_IO_failure_on_the_re_keyed_write(cfg, tmp_path):
    """The BOUNDARY of the never-raises promise, pinned so nobody widens it into a
    silent swallow.

    A refused key is not a raise; a broken `-.jsonl` still is, exactly as a broken
    `w1.jsonl` is for a legal lane. Asserted with the daemon log shaped as a DIRECTORY
    because that is unambiguous — `O_NOFOLLOW` on a symlink and EACCES on a read-only
    directory reach the same place. Swallowing this instead would hide a genuinely broken
    log directory, which is the opposite of what the re-keying is for: keeping the
    evidence that a hostile name reached a writer.
    """
    log = EventLog(cfg.logs_dir)
    (cfg.logs_dir / f"{DAEMON_KEY}.jsonl").mkdir()

    with pytest.raises(OSError):
        log.emit("../../escaped", "turn_start")

    # ...and a legal lane hits the identical surface on its own file, which is the point:
    # the guard introduces no failure mode that did not already exist.
    (cfg.logs_dir / "w1.jsonl").mkdir()
    with pytest.raises(OSError):
        log.emit("w1", "turn_start")


def test_a_caller_field_cannot_mask_the_refusal_stamp(cfg):
    log = EventLog(cfg.logs_dir)
    record = log.emit("../../escaped", "turn_start", log_key_refused=False)
    assert record["log_key_refused"] is True


def test_emit_under_a_legal_name_is_unchanged(cfg, tmp_path):
    """AC#3: the guard must be invisible to every real lane."""
    log = EventLog(cfg.logs_dir)
    for name in LEGAL:
        record = log.emit(name, "turn_start", turn_id=1)
        assert "log_key_refused" not in record
        assert (cfg.logs_dir / f"{name}.jsonl").exists()
        assert log.read(name)[-1]["event"] == "turn_start"
    assert _tree(tmp_path) == set()


def test_the_daemon_key_still_round_trips_through_emit_and_read(cfg):
    """Regression for the guard ITSELF — this is what a naive fix would have broken."""
    log = EventLog(cfg.logs_dir)
    record = log.emit(DAEMON_KEY, "mcp_config_purge_refused", lane="../../outside")
    assert "log_key_refused" not in record
    assert (cfg.logs_dir / f"{DAEMON_KEY}.jsonl").exists()
    assert log.read(DAEMON_KEY)[-1]["lane"] == "../../outside"


@pytest.mark.parametrize("name", ["../../escaped", "/absolute", ".."])
def test_read_and_follow_refuse_a_hostile_key(cfg, name):
    """`server.py` hands both of these a CALLER-SUPPLIED name over the control socket,
    so without the guard in `path()` they were a traversal READ. Unlike `emit` they
    propagate: both callers handle it, and a silent `[]` would read as 'no events'."""
    log = EventLog(cfg.logs_dir)
    with pytest.raises(ValueError, match="invalid event log key"):
        log.read(name)
    with pytest.raises(ValueError, match="invalid event log key"):
        log.path(name)


async def test_follow_refuses_a_hostile_key_before_yielding(cfg):
    log = EventLog(cfg.logs_dir)

    async def _drain():
        async for _ in log.follow("../../escaped", poll_s=0.01):
            return

    # `wait_for`, not a bare await. Found by falsifying this file: with the guard
    # removed, `follow` polls a non-existent file forever and this test HUNG instead of
    # failing — a harness that hangs on a surviving mutant cannot kill it, which is the
    # same class of vacuous harness ECA-135 and ECA-136 both had to discard. The timeout
    # converts the hang into a failure.
    with pytest.raises(ValueError, match="invalid event log key"):
        await asyncio.wait_for(_drain(), timeout=2.0)


def test_read_cannot_be_pointed_at_a_file_outside_the_home(cfg, tmp_path):
    """The traversal READ, demonstrated rather than argued: a real JSONL file planted
    outside the home is reachable by name arithmetic alone."""
    planted = tmp_path / "outside.jsonl"
    planted.write_text(json.dumps({"secret": "operator data"}) + "\n")
    log = EventLog(cfg.logs_dir)

    with pytest.raises(ValueError, match="invalid event log key"):
        log.read("../../outside")


# --- write_capsule -------------------------------------------------------------


def _capsule(capsules_dir, worker):
    return write_capsule(
        capsules_dir,
        worker=worker,
        turn={"id": 1},
        reason="test",
        options_snapshot={},
        events_tail=[],
        stderr_tail=[],
        resume_chain=[None],
    )


@pytest.mark.parametrize("name", HOSTILE)
def test_write_capsule_under_a_hostile_name_writes_nothing_outside_the_home(
    cfg, tmp_path, name
):
    before = _tree(tmp_path)

    with pytest.raises(ValueError, match="invalid worker name"):
        _capsule(cfg.capsules_dir, name)

    assert _tree(tmp_path) == before, "write_capsule escaped the supervisor home"


def test_write_capsule_refuses_before_creating_its_directory(cfg):
    """The guard sits ahead of `mkdir`, so a refused write leaves no trace at all —
    not even the directory it would have written into."""
    assert not cfg.capsules_dir.exists()
    with pytest.raises(ValueError, match="invalid worker name"):
        _capsule(cfg.capsules_dir, "../../escaped")
    assert not cfg.capsules_dir.exists()


def test_write_capsule_under_a_legal_name_is_unchanged(cfg, tmp_path):
    """AC#3, and the mode contract ECA-136 established, both still hold."""
    for name in LEGAL:
        path = _capsule(cfg.capsules_dir, name)
        assert path.exists() and path.parent == cfg.capsules_dir
        assert path.name.startswith(f"{name}-turn1-")
        assert path.stat().st_mode & 0o777 == 0o600
    assert _tree(tmp_path) == set()


@pytest.mark.parametrize("key", sorted(RESERVED_LOG_KEYS))
def test_write_capsule_refuses_every_reserved_key(cfg, key):
    """A capsule always belongs to a real lane; there is no daemon capsule. So this
    writer takes the WORKER predicate, not the log-key one — the looser key must not
    leak through the wrong door."""
    with pytest.raises(ValueError, match="invalid worker name"):
        _capsule(cfg.capsules_dir, key)
