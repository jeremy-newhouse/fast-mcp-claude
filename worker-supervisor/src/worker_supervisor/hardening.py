"""ECA-136: the daemon's own artifacts must not be world-readable, and must not
depend on $HOME's mode for containment.

Every path under the supervisor home is created with a umask-derived mode, which
on both live hosts means 0755 directories and 0644 files — including `state.db`,
which holds every worker's policy (granted MCP credentials verbatim). The only
two paths that were ever hardened are the control socket (0600, server.py) and
ECA-135's per-turn MCP config dir/files (0700/0600).

The creation-site chmods added alongside this module only tighten what the daemon
creates FROM NOW ON. Every artifact on the two live deployments already exists at
the loose mode, and `Path.mkdir(exist_ok=True)` does not touch the mode of a
directory that already exists — so a boot sweep is not belt-and-braces here, it is
the only thing that fixes an existing install.

Symlinked paths are skipped rather than followed, matching the posture
`Engine._write_mcp_config` already takes: same-uid, so not a privilege boundary,
but the daemon should not be the deputy that chmods whatever a link points at.
"""

from __future__ import annotations

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

    def __str__(self) -> str:
        s = f"{self.dirs} dirs 0700, {self.files} files 0600"
        return f"{s}; SKIPPED (symlink): {', '.join(self.skipped)}" if self.skipped else s


def _harden_dir(p: Path, res: HardenResult) -> None:
    if p.is_symlink():
        res.skipped.append(str(p))
        return
    p.mkdir(parents=True, exist_ok=True)
    # Explicit: mkdir's mode is umask-masked AND is not applied at all when the
    # directory already exists — which is every directory on a live install.
    p.chmod(DIR_MODE)
    res.dirs += 1


def _harden_file(p: Path, res: HardenResult) -> None:
    if p.is_symlink():
        res.skipped.append(str(p))
        return
    if not p.exists():
        return
    p.chmod(FILE_MODE)
    res.files += 1


def harden_home(cfg: Config) -> HardenResult:
    """Tighten the supervisor home in place. Idempotent; safe to run every boot.

    MUST run before Registry.connect(), EventLog(...) and the control server:

    * `home` has to be 0700 before `state.db` is first created inside it, and
    * a stale 0644 `state.db-shm` left behind by a crash has to be tightened
      BEFORE SQLite reopens and reuses it — chmod'ing the main db does not
      retroactively fix a sidecar that already exists.

    Unlike ECA-135's MCP-config boot sweep, this runs before the socket preflight
    on purpose and that is safe: every operation here is mkdir-or-chmod, never an
    unlink or a truncate. A second, mistakenly-started daemon would set exactly the
    modes the live daemon already wants — the 2026-07-07 double-daemon hazard was a
    DESTRUCTIVE action racing the guard, which this is not.
    """
    res = HardenResult()
    for d in (cfg.home, cfg.logs_dir, cfg.capsules_dir, cfg.mcp_config_dir):
        _harden_dir(d, res)

    db = cfg.db_path
    # Built by name, not Path.with_suffix: with_suffix would have to be handed the
    # compound '.db-wal' to work at all, which reads like a typo.
    for f in (db, db.parent / f"{db.name}-wal", db.parent / f"{db.name}-shm"):
        _harden_file(f, res)

    # The per-turn MCP config files are already born 0600 under O_EXCL (ECA-135)
    # and engine.start() purges them; the directory mode above is all that is owed.
    for pattern_dir, pattern in ((cfg.logs_dir, "*.jsonl"), (cfg.capsules_dir, "*.json")):
        if pattern_dir.is_symlink():
            continue  # already recorded by _harden_dir
        for f in sorted(pattern_dir.glob(pattern)):
            _harden_file(f, res)

    return res
