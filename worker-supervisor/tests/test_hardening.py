"""ECA-136: the daemon's own artifacts must not be world-readable, and must not
depend on $HOME's mode for containment.

EVERY permission assertion in this file is written to be umask-INDEPENDENT, either
by pre-creating the artifact at the loose mode or by forcing umask 022 around the
call. That is not decoration: this dev host runs umask 077, so a test that lets the
daemon create the artifact fresh gets 0700/0600 for free and passes with the chmod
deleted. Six of nine mutations survived ECA-135's round-2 tests for exactly this
class of reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from worker_supervisor.capsule import write_capsule
from worker_supervisor.events import EventLog
from worker_supervisor.hardening import HardenResult, harden_home
from worker_supervisor.registry import _SCHEMA, Registry

SENTINEL = "FAKE-SENTINEL-ECA136-NOT-A-REAL-CREDENTIAL"


def _mode(p) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@contextlib.contextmanager
def _loose_umask():
    """Force 022 so a tight end-state can only have come from an explicit chmod."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _loose_tree(cfg) -> dict[str, object]:
    """Build a supervisor home in exactly the shape both live hosts are in today:
    0755 dirs, 0644 files, including the sqlite sidecars.

    MULTIPLE files per directory on purpose. With one apiece, a build that hardened
    only the FIRST entry it walked in each directory passed every assertion —
    mbpm2 has ten event logs and six capsules, so that build would have left nine
    and five at 0644 with the suite green. Review caught it; the fixture now makes
    it impossible.

    It also includes shapes the first version of the sweep silently missed: a
    NESTED directory, a file with an unexpected extension, and the operator-owned
    `mcp-configs/`/`secrets/` directories that hold credentials but that no daemon
    code path creates.
    """
    cfg.home.mkdir(parents=True)
    cfg.logs_dir.mkdir()
    cfg.capsules_dir.mkdir()
    nested = cfg.logs_dir / "archive"
    nested.mkdir()
    secrets = cfg.home / "secrets"
    secrets.mkdir()
    db = cfg.db_path
    files = {
        "db": db,
        "wal": db.parent / f"{db.name}-wal",
        "shm": db.parent / f"{db.name}-shm",
        "log1": cfg.logs_dir / "w1.jsonl",
        "log2": cfg.logs_dir / "w2.jsonl",
        "log3": cfg.logs_dir / "_supervisor.jsonl",
        "log_other_ext": cfg.logs_dir / "notes.txt",
        "nested_log": nested / "old.jsonl",
        "capsule1": cfg.capsules_dir / "w1-turn1-20260728T000000Z.json",
        "capsule2": cfg.capsules_dir / "w2-turn9-20260728T000001Z.json",
        "secret": secrets / "greptile.token",
    }
    for p in files.values():
        p.write_text("{}\n")
        p.chmod(0o644)
    for d in (cfg.home, cfg.logs_dir, cfg.capsules_dir, nested, secrets):
        d.chmod(0o755)
    return files


# -- the boot sweep ---------------------------------------------------------


def test_a_pre_existing_loose_home_and_artifact_tree_is_tightened(cfg):
    """The creation-site chmods only tighten what the daemon creates FROM NOW ON.
    Every artifact on both live installs already exists loose, and mkdir(exist_ok)
    does not touch an existing directory's mode — so without this sweep the fix
    reaches neither deployment."""
    files = _loose_tree(cfg)
    assert _mode(cfg.home) == 0o755, "precondition: the tree really is loose"
    assert _mode(files["db"]) == 0o644

    res = harden_home(cfg)

    for d in (cfg.home, cfg.logs_dir, cfg.capsules_dir, cfg.mcp_config_dir,
              cfg.logs_dir / "archive", cfg.home / "secrets"):
        assert _mode(d) == 0o700, d
    for key, p in files.items():
        assert _mode(p) == 0o600, key
    assert res.skipped == [] and res.failed == []
    # Every file in the fixture, counted — not a sample. A sweep that stopped after
    # the first entry per directory would report a smaller number here.
    assert res.files == len(files)


def test_the_sweep_covers_the_sqlite_sidecars_specifically(cfg):
    """Dropping -wal/-shm from the sweep's path list survives the fresh-creation
    test (they inherit the db's mode when SQLite creates them) and fails only
    here, where a crash left them behind at 0644."""
    files = _loose_tree(cfg)
    harden_home(cfg)
    assert _mode(files["wal"]) == 0o600
    assert _mode(files["shm"]) == 0o600


def test_the_sweep_creates_a_missing_home_tight_even_under_a_loose_umask(cfg):
    with _loose_umask():
        harden_home(cfg)
    assert _mode(cfg.home) == 0o700
    assert _mode(cfg.logs_dir) == 0o700
    assert _mode(cfg.capsules_dir) == 0o700


def test_the_sweep_skips_symlinks_rather_than_chmodding_through_them(cfg, tmp_path):
    """Same posture as Engine._write_mcp_config: same-uid, so not a privilege
    boundary, but the daemon should not be the deputy that chmods whatever a link
    points at."""
    outside = tmp_path / "outside"
    outside.mkdir()
    # The target must CONTAIN something. With an empty directory, a build walking
    # with followlinks=True has nothing to chmod inside it and passes anyway —
    # review caught exactly that, and the mutation now dies on these two files.
    victim_file = outside / "someone-elses.jsonl"
    victim_file.write_text("{}\n")
    victim_file.chmod(0o644)
    victim_dir = outside / "nested"
    victim_dir.mkdir()
    outside.chmod(0o755)
    victim_dir.chmod(0o755)
    cfg.home.mkdir(parents=True)
    cfg.home.chmod(0o755)
    cfg.logs_dir.symlink_to(outside)

    res = harden_home(cfg)

    assert _mode(outside) == 0o755, "the symlink target was chmod'd!"
    assert _mode(victim_file) == 0o644, "a file INSIDE the symlink target was chmod'd!"
    assert _mode(victim_dir) == 0o755, "a dir INSIDE the symlink target was chmod'd!"
    assert str(cfg.logs_dir) in res.skipped


def test_the_sweep_skips_a_symlinked_state_db(cfg, tmp_path):
    victim = tmp_path / "victim.db"
    victim.write_text("x")
    victim.chmod(0o644)
    cfg.home.mkdir(parents=True)
    cfg.db_path.symlink_to(victim)

    res = harden_home(cfg)

    assert _mode(victim) == 0o644, "the symlink target was chmod'd!"
    assert str(cfg.db_path) in res.skipped


def test_a_path_that_cannot_be_chmodded_is_reported_not_fatal(cfg, monkeypatch):
    """harden_home runs FIRST in _serve() — ahead of the registry, the event log and
    the socket preflight — and a boot failure in this daemon is a 30s lazy-fail
    RETRY. So one un-chmoddable artifact (root-owned after a restore, `uchg`, a
    hostile mount) escaping from here would take the whole lane fleet down
    permanently, which is strictly worse than the loose mode it was trying to fix.
    Pins BOTH halves: it must not raise, and it must not fail silently either."""
    files = _loose_tree(cfg)
    victim = files["log2"]
    real_chmod = Path.chmod

    def flaky(self, mode, *a, **k):
        if self.name == victim.name:
            raise PermissionError(13, "Operation not permitted")
        return real_chmod(self, mode, *a, **k)

    monkeypatch.setattr(Path, "chmod", flaky)
    res = harden_home(cfg)  # must NOT raise

    assert any(victim.name in f for f in res.failed), f"not reported: {res.failed}"
    assert _mode(files["log1"]) == 0o600, "the rest of the sweep must still run"
    assert _mode(cfg.home) == 0o700
    assert _mode(victim) == 0o644, "precondition: the victim really was not tightened"


def test_the_sweep_is_idempotent(cfg):
    _loose_tree(cfg)
    first = harden_home(cfg)
    second = harden_home(cfg)
    assert (second.dirs, second.files) == (first.dirs, first.files)
    assert _mode(cfg.home) == 0o700 and _mode(cfg.db_path) == 0o600


# -- state.db and its sidecars ----------------------------------------------


async def test_a_fresh_state_db_and_its_sidecars_are_0600(cfg):
    """Pins the chmod's PRESENCE. It deliberately does NOT claim to pin its
    position: mutation testing showed the chmod can be moved after
    `PRAGMA journal_mode=WAL`, or even after the first write, and the sidecars
    still come back 0600 — SQLite gives a fresh sidecar the main db's mode, and
    the reclaim's TRUNCATE checkpoint recreates them after the chmod either way.
    An earlier version of this docstring asserted the opposite."""
    cfg.home.mkdir(parents=True)
    reg = Registry(cfg.db_path)
    with _loose_umask():
        await reg.connect()
        try:
            db = cfg.db_path
            assert _mode(db) == 0o600
            for suffix in ("-wal", "-shm"):
                side = db.parent / f"{db.name}{suffix}"
                assert side.exists(), f"{suffix} was never created"
                assert _mode(side) == 0o600, suffix
        finally:
            await reg.close()


async def test_connect_tightens_pre_existing_loose_sidecars_without_the_sweep(cfg):
    """The case where Registry's own chmods are the ONLY thing that helps.

    For a FRESH database the two mechanisms are redundant — harden_home tightens
    the home, and SQLite gives a sidecar created afterwards the main db's mode, so
    dropping Registry's chmods entirely still yields 0600 and the test above cannot
    tell the difference (three mutants survived it, correctly). Inheritance does
    NOT apply to a sidecar that already exists, so this pins the real job:
    Registry.connect() called on a loose install with no sweep in front of it."""
    cfg.home.mkdir(parents=True)
    db = cfg.db_path
    names = (db.name, f"{db.name}-wal", f"{db.name}-shm")

    # A REAL connection to produce real sidecars. Hand-writing empty -wal/-shm files
    # instead wedges SQLite outright (it blocks trying to recover them) — the same
    # trap that hung an earlier probe in this task. Held open so the sidecars still
    # exist when the second Registry connects; a clean close removes them.
    holder = Registry(db)
    await holder.connect()
    try:
        for name in names:
            (db.parent / name).chmod(0o644)
        assert _mode(db) == 0o644, "precondition: the install really is loose"

        reg = Registry(db)
        await reg.connect()  # deliberately no harden_home() in front of it
        try:
            for name in names:
                p = db.parent / name
                assert p.exists(), name
                assert _mode(p) == 0o600, name
        finally:
            await reg.close()
    finally:
        await holder.close()


async def test_connect_does_not_chmod_through_a_symlinked_state_db(cfg, tmp_path):
    victim = tmp_path / "victim.db"
    victim.write_bytes(b"")
    victim.chmod(0o644)
    cfg.home.mkdir(parents=True)
    cfg.db_path.symlink_to(victim)

    reg = Registry(cfg.db_path)
    await reg.connect()
    try:
        assert _mode(victim) == 0o644, "the symlink target was chmod'd!"
    finally:
        await reg.close()


async def test_secure_delete_is_on_not_fast(cfg):
    """ON (1), not FAST (2): FAST deliberately leaves freelist pages unzeroed, so
    asserting `!= 0` would let the weaker setting survive."""
    cfg.home.mkdir(parents=True)
    reg = Registry(cfg.db_path)
    await reg.connect()
    try:
        cur = await reg.db.execute("PRAGMA secure_delete")
        assert (await cur.fetchone())[0] == 1
    finally:
        await reg.close()


async def _fill_and_delete(reg, n: int = 40) -> None:
    for i in range(n):
        await reg.spawn_worker(
            f"w{i}",
            "/tmp/r",
            {"mcp_servers": {"s": {"headers": {"Authorization": f"Bearer {SENTINEL}" * 40}}}},
        )
    for i in range(n):
        assert await reg.delete_worker(f"w{i}") is True


def _sentinel_bytes(cfg) -> int:
    db = cfg.db_path
    total = 0
    for p in (db, db.parent / f"{db.name}-wal", db.parent / f"{db.name}-shm"):
        if p.exists():
            total += p.read_bytes().count(SENTINEL.encode())
    return total


async def test_a_deleted_workers_policy_is_not_recoverable_from_the_file_bytes(cfg):
    """The behavioural test. `PRAGMA secure_delete` echoing back 1 only proves the
    statement executed; this proves it did something.

    Asserted while the connection is still OPEN, which is the whole point and was
    the bug review found in the first version. secure_delete zeroes the freed cell
    in a NEW page image, and in WAL mode that image goes to state.db-wal while the
    PRE-delete image stays there untouched: measured 900 sentinel occurrences still
    in the -wal after deleting every worker. close() checkpoints and removes the
    WAL, so asserting after it passed even with delete_worker's checkpoint deleted.
    The daemon holds this connection for its entire uptime and never closes it, so
    the open-connection state is the only one that describes production."""
    cfg.home.mkdir(parents=True)
    reg = Registry(cfg.db_path)
    await reg.connect()
    await _fill_and_delete(reg)
    try:
        assert _sentinel_bytes(cfg) == 0, (
            "a revoked grant is still recoverable from the files while the daemon runs"
        )
    finally:
        await reg.close()

    assert _sentinel_bytes(cfg) == 0


# -- the one-shot reclaim of pages freed BEFORE the fix ---------------------


def _leaky_db(cfg, n: int = 40) -> int:
    """Build a db in the state both live installs are in: secure_delete never set,
    user_version 0, and the policies of deleted workers sitting in freed pages."""
    cfg.home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cfg.db_path)
    con.execute("PRAGMA journal_mode=WAL")
    assert con.execute("PRAGMA secure_delete").fetchone()[0] == 0, (
        "precondition: this sqlite build must default secure_delete OFF, or the "
        "fixture cannot produce the leak it is meant to reproduce"
    )
    con.executescript(_SCHEMA)
    blob = f'{{"h":{{"Authorization":"Bearer {SENTINEL * 40}"}}}}'
    for i in range(n):
        con.execute(
            "INSERT INTO workers (name, repo, status, policy, created_at, updated_at,"
            " last_active_at) VALUES (?, ?, 'idle', ?, '', '', '')",
            (f"w{i}", "/tmp/r", blob),
        )
    con.commit()
    for i in range(n):
        con.execute("DELETE FROM workers WHERE name = ?", (f"w{i}",))
    con.commit()
    con.close()
    return _sentinel_bytes(cfg)


async def test_the_one_shot_reclaim_scrubs_a_pre_existing_leak_out_of_the_db_file(cfg):
    """The load-bearing test for the trailing wal_checkpoint(TRUNCATE): VACUUM's
    rebuilt output lands in the WAL first, so after checkpoint+VACUUM alone the
    logical database is clean while the .db file on disk still holds every old page.

    The assertion MUST be made while the connection is still OPEN. Closing a
    connection checkpoints and removes the WAL, which scrubs the file as a side
    effect — so asserting after close() passes with the trailing checkpoint deleted
    (confirmed: that mutant survived the first version of this test). The open-
    connection state is also the only one that matters in production: the daemon
    holds this connection for its entire uptime and never closes it."""
    leaked = _leaky_db(cfg)
    assert leaked > 0, "precondition: the fixture must actually leak, or this is vacuous"

    reg = Registry(cfg.db_path)
    await reg.connect()
    try:
        assert _sentinel_bytes(cfg) == 0, (
            "still recoverable from the files while the daemon holds the connection"
        )
        # Integrity + parity, so a mutation that simply empties the file cannot pass.
        cur = await reg.db.execute("PRAGMA integrity_check")
        assert (await cur.fetchone())[0] == "ok"
        cur = await reg.db.execute("SELECT count(*) FROM workers")
        assert (await cur.fetchone())[0] == 0
        cur = await reg.db.execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0] == "wal", "WAL must survive the reclaim"
    finally:
        await reg.close()

    assert _sentinel_bytes(cfg) == 0


async def test_the_reclaim_preserves_live_rows(cfg):
    """A VACUUM that dropped data would pass a bytes-are-gone assertion trivially."""
    cfg.home.mkdir(parents=True)
    reg = Registry(cfg.db_path)
    await reg.connect()
    await reg.spawn_worker("keeper", "/tmp/r", {"model": "sonnet"})
    await reg.close()

    reg2 = Registry(cfg.db_path)
    await reg2.connect()
    try:
        w = await reg2.get_worker("keeper")
        assert w is not None and w["repo"] == "/tmp/r"
    finally:
        await reg2.close()


async def test_the_reclaim_marks_itself_done_and_leaves_the_mode_intact(cfg):
    """VACUUM preserves the file's mode and user_version, so the chmod survives it.

    (An earlier version of this test also built a `seen` list and an execute spy
    that it never asserted on, and which was installed AFTER connect() had already
    run the reclaim — so it could not have observed it. Review flagged it as
    reading like coverage while pinning nothing; the real coverage is
    test_the_reclaim_vacuums_on_the_first_boot_and_never_again.)"""
    cfg.home.mkdir(parents=True)
    reg = Registry(cfg.db_path)
    with _loose_umask():
        await reg.connect()
    try:
        cur = await reg.db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == 1
    finally:
        await reg.close()

    assert _mode(cfg.db_path) == 0o600


async def test_a_failing_reclaim_is_recorded_and_never_fatal(cfg, monkeypatch):
    """The reclaim is a one-shot remediation, not a boot requirement — but a silent
    failure would leave old credentials in the file while the boot looked clean.
    Pins both halves: connect() still succeeds, AND reclaim_error is set."""
    cfg.home.mkdir(parents=True)

    async def boom(self):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Registry, "_checkpoint_truncate", boom)
    reg = Registry(cfg.db_path)
    await reg.connect()  # must NOT raise
    try:
        assert reg.reclaim_error is not None
        assert "OperationalError" in reg.reclaim_error
        cur = await reg.db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == 0, "must stay 0 so the next boot retries"
    finally:
        await reg.close()


async def test_a_healthy_reclaim_records_no_error(cfg):
    """Negative control: without it, a build that ALWAYS set reclaim_error would
    pass the test above."""
    cfg.home.mkdir(parents=True)
    reg = Registry(cfg.db_path)
    await reg.connect()
    try:
        assert reg.reclaim_error is None
    finally:
        await reg.close()


def test_serve_hardens_the_home_before_touching_anything_else(monkeypatch, cfg):
    """The ONE line that reaches both live installs is `harden_home(cfg)` in
    _serve(). Nothing tested that it is called: deleting it left 118/118 green while
    shipping a daemon that boots normally and hardens nothing. Also pins the
    ORDERING — it must run before the registry connects, which is when state.db is
    created inside the home."""
    import worker_supervisor.__main__ as main_mod

    order: list[str] = []
    monkeypatch.setattr(main_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(main_mod, "harden_home",
                        lambda c: (order.append("harden"), HardenResult())[1])

    class StopHere(Exception):
        pass

    class FakeRegistry:
        def __init__(self, *a, **k):
            order.append("registry")
            raise StopHere

    monkeypatch.setattr(main_mod, "Registry", FakeRegistry)
    with pytest.raises(StopHere):
        asyncio.run(main_mod._serve())

    assert order == ["harden", "registry"]


async def _connect_recording_reclaim_sql(reg, seen: list[str]) -> None:
    """Connect, recording every statement the reclaim issues. The spy is installed
    on the live connection at the top of the reclaim, so it sees exactly the SQL
    the reclaim decides to run and nothing else."""
    cls = type(reg)
    real_reclaim = cls._reclaim_freed_pages

    async def reclaim_with_spy(self):
        real_execute = self._db.execute

        def spy(sql, *a, **k):
            seen.append(str(sql))
            return real_execute(sql, *a, **k)

        self._db.execute = spy
        try:
            return await real_reclaim(self)
        finally:
            self._db.execute = real_execute

    cls._reclaim_freed_pages = reclaim_with_spy
    try:
        await reg.connect()
    finally:
        cls._reclaim_freed_pages = real_reclaim


async def test_the_reclaim_vacuums_on_the_first_boot_and_never_again(cfg):
    """Asserts on the SQL actually executed. An earlier version recorded only the
    user_version value it read back — which a build with the guard DELETED also
    satisfies, so that mutant survived. Whether VACUUM runs is the observable that
    actually distinguishes them."""
    cfg.home.mkdir(parents=True)

    first: list[str] = []
    reg = Registry(cfg.db_path)
    await _connect_recording_reclaim_sql(reg, first)
    await reg.close()
    assert any("VACUUM" in s.upper() for s in first), (
        f"the first boot must reclaim; saw: {first}"
    )

    second: list[str] = []
    reg2 = Registry(cfg.db_path)
    await _connect_recording_reclaim_sql(reg2, second)
    try:
        assert any("user_version" in s for s in second), "the guard must be consulted"
        assert not any("VACUUM" in s.upper() for s in second), (
            f"the one-shot guard must short-circuit on a second boot; saw: {second}"
        )
    finally:
        await reg2.close()


# -- event logs -------------------------------------------------------------


def test_the_events_dir_and_a_pre_existing_loose_log_are_tightened(cfg):
    """O_CREAT's mode is IGNORED for a file that already exists, and every log file
    on both live hosts already exists at 0644 — so the fchmod is required, not
    belt-and-braces. Deleting it must fail this test."""
    cfg.logs_dir.mkdir(parents=True)
    cfg.logs_dir.chmod(0o755)
    stale = cfg.logs_dir / "w1.jsonl"
    stale.write_text('{"pre":"existing"}\n')
    stale.chmod(0o644)

    log = EventLog(cfg.logs_dir)
    log.emit("w1", "turn_started", turn_id=1)

    assert _mode(cfg.logs_dir) == 0o700
    assert _mode(stale) == 0o600
    assert len(log.read("w1")) == 2, "the pre-existing record must survive the append"


def test_the_events_mode_does_not_depend_on_the_open_mode(cfg, monkeypatch):
    """Mirrors ECA-135's test of the same shape: force os.open to ask for a loose
    mode and neutralise the host umask, so only the explicit fchmod can produce
    0600."""
    cfg.logs_dir.mkdir(parents=True)
    real_open, real_umask = os.open, os.umask

    def loose_open(path, flags, mode=0o777):
        return real_open(path, flags, 0o666)

    old_umask = os.umask(0)
    monkeypatch.setattr(os, "umask", lambda m: old_umask)  # keep pytest teardown honest
    monkeypatch.setattr(os, "open", loose_open)
    try:
        EventLog(cfg.logs_dir).emit("w1", "turn_started", turn_id=1)
    finally:
        monkeypatch.undo()
        real_umask(old_umask)

    assert _mode(cfg.logs_dir / "w1.jsonl") == 0o600


def test_the_creation_sites_do_not_chmod_through_a_symlinked_directory(cfg, tmp_path):
    """harden_home reports a symlinked logs/ or capsules/ as SKIPPED and refuses to
    chmod the target. EventLog.__init__ and write_capsule run a few lines later in
    the same boot and would have re-moded it anyway — the two must not disagree
    about the posture, or the sweep's refusal is decorative."""
    outside_logs = tmp_path / "elsewhere-logs"
    outside_caps = tmp_path / "elsewhere-capsules"
    for d in (outside_logs, outside_caps):
        d.mkdir()
        d.chmod(0o755)
    cfg.home.mkdir(parents=True)
    cfg.logs_dir.symlink_to(outside_logs)
    cfg.capsules_dir.symlink_to(outside_caps)

    EventLog(cfg.logs_dir)
    _capsule(cfg)

    assert _mode(outside_logs) == 0o755, "EventLog chmod'd through the symlink!"
    assert _mode(outside_caps) == 0o755, "write_capsule chmod'd through the symlink!"


def test_emit_refuses_to_write_through_a_symlink(cfg, tmp_path):
    cfg.logs_dir.mkdir(parents=True)
    victim = tmp_path / "victim.jsonl"
    victim.write_text("")
    (cfg.logs_dir / "w1.jsonl").symlink_to(victim)

    with pytest.raises(OSError):
        EventLog(cfg.logs_dir).emit("w1", "turn_started", turn_id=1)
    assert victim.read_text() == "", "the symlink target was written through!"


# -- failure capsules -------------------------------------------------------


def _capsule(cfg, worker="w1", turn_id=1):
    return write_capsule(
        cfg.capsules_dir,
        worker=worker,
        turn={"id": turn_id, "prompt": "p"},
        reason="error",
        options_snapshot={},
        events_tail=[],
        stderr_tail=["a line"],
        resume_chain=[None],
    )


def test_a_capsule_is_0600_in_a_0700_dir_even_under_a_loose_umask(cfg):
    with _loose_umask():
        path = _capsule(cfg)
    assert _mode(path) == 0o600
    assert _mode(cfg.capsules_dir) == 0o700


def test_the_capsule_mode_does_not_depend_on_the_open_mode(cfg, monkeypatch):
    """Kills the 'drop the fchmod' mutant, which the end-state test above does NOT:
    under umask 022 the O_CREAT mode alone already yields 0600, so that test passes
    with the fchmod deleted. Force os.open to ask for a loose mode and neutralise
    the umask, and only the explicit fchmod can produce 0600. (Same shape as the
    events test above, and as ECA-135's own.)"""
    cfg.capsules_dir.mkdir(parents=True)
    real_open, real_umask = os.open, os.umask

    def loose_open(path, flags, mode=0o777):
        return real_open(path, flags, 0o666)

    old_umask = os.umask(0)
    monkeypatch.setattr(os, "umask", lambda m: old_umask)  # keep pytest teardown honest
    monkeypatch.setattr(os, "open", loose_open)
    try:
        path = _capsule(cfg)
    finally:
        monkeypatch.undo()
        real_umask(old_umask)

    assert _mode(path) == 0o600


def test_a_pre_existing_loose_capsules_dir_is_tightened(cfg):
    """Dropping capsules_dir.chmod(0o700) survives the fresh-creation test on this
    umask-077 host and fails only here."""
    cfg.capsules_dir.mkdir(parents=True)
    cfg.capsules_dir.chmod(0o755)
    _capsule(cfg)
    assert _mode(cfg.capsules_dir) == 0o700


def test_a_capsule_refuses_to_write_through_a_pre_planted_path(cfg, tmp_path):
    """O_EXCL: the capsule filename is predictable (worker, turn id, and a
    timestamp at one-second granularity), so a pre-planted hard link would
    otherwise receive the prompt and the stderr tail."""
    cfg.capsules_dir.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("original")
    first = _capsule(cfg)
    first.unlink()
    os.link(victim, first)  # hard link, exactly what O_NOFOLLOW does not catch

    with pytest.raises(FileExistsError):
        _capsule(cfg)
    assert victim.read_text() == "original", "the planted link took the payload!"


def test_two_capsules_for_one_turn_in_the_same_second_raise(cfg):
    """A deliberate behaviour change from write_text's silent overwrite. The caller
    treats it as non-fatal — Engine._finish_failure_capsule catches and emits
    failure_capsule_error."""
    cfg.capsules_dir.mkdir(parents=True)
    _capsule(cfg)
    with pytest.raises(FileExistsError):
        _capsule(cfg)


async def test_the_engine_treats_a_capsule_collision_as_non_fatal(cfg, registry, events):
    """Pins the claim the capsule docstring makes about its caller."""
    from worker_supervisor.engine import Engine
    from worker_supervisor.gate import QuestionBridge

    eng = Engine(cfg, registry, events, QuestionBridge(registry, events))
    await registry.spawn_worker("w1", "/tmp/r", {})
    tid = await registry.enqueue_turn("w1", "p")
    await eng._finish_failure_capsule("w1", tid, "error", {}, ["x"], [None])
    await eng._finish_failure_capsule("w1", tid, "error", {}, ["x"], [None])

    kinds = [e["event"] for e in events.read("w1")]
    assert "failure_capsule" in kinds and "failure_capsule_error" in kinds


# -- the config the sweep reads --------------------------------------------


def test_the_sweep_follows_a_relocated_home(cfg, tmp_path):
    """SUPERVISOR_HOME is configurable; the sweep must harden the configured home,
    not a hardcoded ~/.worker-supervisor."""
    elsewhere = tmp_path / "relocated"
    moved = replace(cfg, home=elsewhere)
    harden_home(moved)
    assert _mode(elsewhere) == 0o700
    assert _mode(elsewhere / "logs") == 0o700
