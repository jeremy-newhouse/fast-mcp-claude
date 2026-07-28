"""ECA-136: the daemon's own artifacts must not be world-readable, and must not
depend on $HOME's mode for containment.

Every path the daemon creates under the supervisor home gets a umask-derived mode,
which on both live hosts means 0755 directories and 0644 files — including
`state.db`, which holds every worker's policy (granted MCP credentials verbatim).
The only daemon-created paths that were ever hardened are the control socket
(0600, server.py) and ECA-135's per-turn MCP config dir/files (0700/0600). Two
credential-bearing directories here are created by the OPERATOR, not by any code
path in this package — `mcp-configs/` and `secrets/` — and the sweep below covers
them anyway, because it walks the home rather than a list of names it knows.

The creation-site chmods added alongside this module are unconditional, so several
of them DO also tighten an existing artifact: `EventLog.__init__` chmods logs/ on
every construction, `write_capsule` chmods capsules/ on every write, and
`Registry.connect` chmods state.db and its sidecars on every boot. What they cannot
reach is anything they do not themselves touch — the supervisor HOME directory
(nothing creates it after first boot), and the per-worker logs and capsules ALREADY
on disk, which are only ever opened for append or written under a new name. On both
live installs that is ten event logs and six capsules sitting at 0644. That is what
the sweep is for; it is not belt-and-braces, and it is also not the only mechanism.

Symlinked paths are skipped rather than followed, matching the posture
`Engine._write_mcp_config` already takes: same-uid, so not a privilege boundary,
but the daemon should not be the deputy that chmods whatever a link points at.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

DIR_MODE = 0o700
FILE_MODE = 0o600


@dataclass
class HardenResult:
    dirs: int = 0
    files: int = 0
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        s = f"{self.dirs} dirs 0700, {self.files} files 0600"
        if self.skipped:
            s += f"; SKIPPED (symlink): {', '.join(self.skipped)}"
        if self.failed:
            s += f"; FAILED: {', '.join(self.failed)}"
        return s


def _harden_dir(p: Path, res: HardenResult) -> None:
    if p.is_symlink():
        res.skipped.append(str(p))
        return
    try:
        p.mkdir(parents=True, exist_ok=True)
        # Explicit: mkdir's mode is umask-masked AND is not applied at all when the
        # directory already exists — which is every directory on a live install.
        p.chmod(DIR_MODE)
    except OSError as e:
        res.failed.append(f"{p} ({e.__class__.__name__})")
        return
    res.dirs += 1


def _harden_file(p: Path, res: HardenResult) -> None:
    if p.is_symlink():
        res.skipped.append(str(p))
        return
    try:
        if not p.exists():
            return
        p.chmod(FILE_MODE)
    except OSError as e:
        res.failed.append(f"{p} ({e.__class__.__name__})")
        return
    res.files += 1


def harden_home(cfg: Config) -> HardenResult:
    """Tighten the supervisor home in place. Idempotent; safe to run every boot.

    Runs before Registry.connect() so that `home` is 0700 before `state.db` is
    first created inside it, which is a real ordering constraint but a narrow one:
    it removes a window, it does not enable anything. Deliberately NOT claimed:
    that a stale 0644 `state.db-shm` "must" be tightened before SQLite reopens it.
    An earlier version said so and it is false — chmod'ing a reused sidecar after
    SQLite has opened it works and sticks, and `Registry._tighten_db_files` does
    exactly that on every boot regardless of this sweep.

    Unlike ECA-135's MCP-config boot sweep, this runs before the socket preflight
    on purpose and that is safe: every operation here is mkdir-or-chmod, never an
    unlink or a truncate. A second, mistakenly-started daemon would set exactly the
    modes the live daemon already wants — the 2026-07-07 double-daemon hazard was a
    DESTRUCTIVE action racing the guard, which this is not.

    Never raises. It runs FIRST in _serve(), ahead of the registry, the event log
    and the socket preflight, so an exception here is a boot failure — and boot
    failures in this daemon are a 30s lazy-fail RETRY, i.e. an un-chmoddable file
    (root-owned after a restore, `uchg`, a hostile mount) would take the whole lane
    fleet down permanently. Hardening is best-effort by construction: a path it
    cannot tighten is reported in `failed` for the caller to log, and the boot
    continues to a daemon that works.
    """
    res = HardenResult()
    # Create the daemon's own directories if missing, so a FRESH install is hardened
    # from first boot instead of inheriting whatever mode their eventual creator
    # uses. Hardening itself happens in the single walk below.
    for d in (cfg.home, cfg.logs_dir, cfg.capsules_dir, cfg.mcp_config_dir):
        if d.is_symlink():
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            res.failed.append(f"{d} ({e.__class__.__name__})")

    _harden_dir(cfg.home, res)
    if cfg.home.is_symlink():
        return res

    # Everything under the home, recursively. Deliberately NOT a list of known names
    # or extensions: an earlier version swept state.db + logs/*.jsonl +
    # capsules/*.json, which silently missed anything nested, anything with another
    # extension, and the operator-owned `mcp-configs/` and `secrets/` directories
    # that also live here and also hold credentials. The set of things under this
    # directory is not closed, so enumerate what is actually there.
    #
    # os.walk rather than Path.rglob: it takes followlinks=False explicitly (rglob's
    # symlink-descent behaviour has changed across Python versions), which is the
    # posture this module documents — report a link, never chmod through it.
    #
    # Only regular files and directories are touched. The control socket is already
    # 0600 (server.py) and chmod'ing a live socket is not this function's business.
    for root, dirnames, filenames in os.walk(cfg.home, followlinks=False):
        base = Path(root)
        # The symlink decision lives in _harden_dir/_harden_file, not here: one
        # owner per guard. Pre-checking it at the call site too would leave the
        # helpers' own checks unreachable, and an unfalsifiable branch is exactly
        # what this change removed elsewhere.
        for name in sorted(dirnames):
            _harden_dir(base / name, res)
        for name in sorted(filenames):
            _harden_file(base / name, res)

    return res
