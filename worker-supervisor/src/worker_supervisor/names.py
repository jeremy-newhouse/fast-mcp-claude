"""Worker-name validation — the one predicate every path derivation goes through.

A worker name is used VERBATIM as a filename: the lane's event log
(`logs/<name>.jsonl`), its failure capsules (`capsules/<name>-turn<id>-<ts>.json`)
and, since ECA-135, its MCP credential file. So the name is a path component, and
an unvalidated one is a file-creation primitive pointed anywhere the daemon's uid
can write.

Why this lives in its own leaf module (ECA-137). The validator used to sit in
`engine.py`, which imports both `events` and `capsule` — so those two modules could
not call it back without a cycle, and their derivations stayed unguarded through
ECA-135 and ECA-136. Keeping it here, importing nothing from the package, means the
lowest-level writers can hold the same guard as the engine.

Three predicates, not one, and the differences are load-bearing:

* `require_safe_worker_name` — for a real lane. Its MESSAGE is byte-identical to
  ECA-135's, because operators read it and tests assert on it. Its BEHAVIOUR is not
  quite unchanged, and saying "unchanged" was a review finding: a non-str argument used
  to raise TypeError out of `re.fullmatch` and now raises ValueError. That is deliberate
  and load-bearing (see `is_safe_worker_name`), not an accident of the move.
* `require_safe_log_key` — for an EventLog key, which is a SUPERSET: the daemon owns
  two streams of its own, `RESERVED_LOG_KEYS`, and NEITHER satisfies the worker-name
  pattern (which requires an alphanumeric first character). Validating an event key
  with the worker predicate would refuse the daemon's own channels — `-`, which carries
  ECA-135's `mcp_config_purge_refused`, the event whose entire purpose is to report a
  refused name without writing through it, and `_supervisor`, which carries the live
  mesh-announce loop on both supervisor hosts.
* `is_safe_hook_script` (ECA-140) — for a repo guard-hook FILENAME, a third name-space
  again. See `HOOK_SCRIPT_RE` for the one character class that differs and the evidence
  for it.

All three are total over their argument's TYPE as well as its value, because the control
socket hands JSON through uncoerced. See `is_safe_worker_name`.
"""

from __future__ import annotations

import re

# Leading char excludes '.', so no dotfiles and no '..'. fullmatch, NOT `$`: `$` also
# matches before a trailing newline, so "Ultra1\n" would pass and produce a filename
# with an embedded newline.
WORKER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# A repo guard-hook filename (ECA-140). Identical to WORKER_NAME_RE except that the
# LEADING class also admits '_' — the whole difference, and the reason this is a separate
# pattern instead of a reuse. Evidence and the measurement behind it: `is_safe_hook_script`.
# '.' is still excluded from the leading class, which is what rules out dotfiles, '.' and
# '..'; a separator is excluded from both classes, which is what rules out every other
# traversal shape including a bare absolute path (see gate._run_guard_hook — an absolute
# `script` needs no '..' at all, because Path.__truediv__ discards its left operand).
# fullmatch, NOT '$', for the same trailing-newline reason as above.
HOOK_SCRIPT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")

# Keys the DAEMON writes its own event streams under, not lanes. Both deliberately fail
# WORKER_NAME_RE, so neither can ever collide with a real worker — `Engine.spawn` would
# refuse a lane by either name.
#
# `_supervisor` is live on both supervisor hosts and was very nearly a regression here:
# the first version of this guard admitted only DAEMON_KEY, which would have refused
# `presence.py`'s three announce-loop emits, stopped appending to the existing
# `logs/_supervisor.jsonl`, and made `workers events _supervisor` raise. The ENTIRE suite
# stayed green through that (no count given on purpose — three different numbers were
# quoted for the same moment in review), because nothing covered a presence emit: the same
# shape as ECA-136's finding that no test proved the daemon ever CALLED harden_home. Found
# by listing the live logs/ directories, not by reasoning about the code.
#
# The two keys are NOT symmetric on disk, which review caught being overstated elsewhere:
# `_supervisor.jsonl` exists on both hosts and is written every announce beat, while
# `-.jsonl` exists on NEITHER — `mcp_config_purge_refused`/`mcp_config_sweep_failed` have
# never fired in production, so the first refused emit will create it.
DAEMON_KEY = "-"
SUPERVISOR_STREAM = "_supervisor"
RESERVED_LOG_KEYS = frozenset({DAEMON_KEY, SUPERVISOR_STREAM})


def is_safe_worker_name(name: object) -> bool:
    """Total over its argument, including the type — deliberately.

    `server.py` hands control-socket JSON straight through (`args["name"]`) with no
    coercion, so a caller can send `{"name": 123}` or `null`. Against a `str` pattern
    those raise TypeError, and TypeError is not what any caller here is written to
    expect: `EventLog.emit` catches ValueError specifically, so a non-str key would
    escape it and re-create the lane wedge the re-keying exists to prevent. A wrong
    TYPE is just another unsafe name.
    """
    if not isinstance(name, str):
        return False
    return bool(WORKER_NAME_RE.fullmatch(name)) and ".." not in name


def require_safe_worker_name(name: object) -> None:
    """Reject any name that is not a plain filename component.

    Typed `object`, not `str`, and that is not laxness: these are the entry points for
    untrusted control-socket JSON, so a non-str genuinely arrives here and is genuinely
    handled (see `is_safe_worker_name`). Annotating `str` would be a claim the runtime
    does not make. No type checker runs on this project, so the annotation's only job is
    to tell the truth to a reader.

    Called from `Engine.spawn` and from EVERY path derivation: validating only at
    spawn leaves the guard at the wrong layer, because `remove`, the turn-end purge
    and boot recovery all consume a PERSISTED name as a path, and a row written
    before this validator existed is not covered by it.
    """
    if not is_safe_worker_name(name):
        raise ValueError(
            f"invalid worker name {name!r}: must match {WORKER_NAME_RE.pattern} "
            "and contain no '..' (the name is used as a filename)"
        )


def is_safe_hook_script(script: object) -> bool:
    """True if `script` is a plain filename component safe to join under `.claude/hooks/`.

    Why a THIRD predicate rather than reusing the worker one (ECA-140). The task that
    filed this guessed `WORKER_NAME_RE` "probably fits" and asked for it to be checked
    against the hook filenames actually in use before being reused. It does not fit. Two
    measurements, both against this operator's real hosts, and both taken from the `repo`
    column of each live `workers` row — i.e. the value that actually becomes `repo_root`
    here, not a guess at where worker repos live:

    * No guard hook is CONFIGURED anywhere. All 15 live worker rows (9 on mini2, 6 on
      mbpm2) carry `guard_hooks={}`, so nothing deployed changes behaviour either way.
    * The hooks directories themselves are very much in use: 6 of those 15 rows have one
      (mbpm2's `ultra1/2/3/5/6` on `evolv-ultra`, 23 scripts each; mini2's `eca72` on
      `fast-mcp-claude`, one). 24 distinct filenames between them. `HOOK_SCRIPT_RE`
      refuses 0 of the 24; `WORKER_NAME_RE` refuses exactly one, `_common.sh`, because it
      demands an alphanumeric FIRST character. Which is ECA-137's `_supervisor` finding
      arriving a second time in a new name-space — a leading underscore means "shared
      helper, not an entry point" to a shell-script author, so it is idiomatic here in a
      way it never is for a lane name.

    An earlier version of this docstring said no worker repo had a hooks directory at
    all, and reached the same conclusion from 47 filenames sampled out of `~/repos`. The
    claim was false — the glob behind it stopped one level short of the real repo roots,
    which are `~/worker-repos/<lane>/<repo>` — and the sample it fell back on was not the
    deployed corpus. Review caught it. Recorded because the correction runs the other way
    for once: `_common.sh` is not an incidental name from some unrelated checkout, it is
    live in five worker repos this daemon spawns lanes against today.

    So the pattern is the worker one with `_` added to the leading class, and nothing
    else. It refuses 0 of the 47 and every traversal shape, because the shapes that
    traverse need a separator or a leading dot and neither is in the charset.

    Total over its argument's TYPE, like its siblings and for the same reason: `server.py`
    passes control-socket JSON through uncoerced, so `guard_hooks={"Bash": 123}` really
    does arrive here, and before this guard existed it raised TypeError out of
    `Path.__truediv__` INSIDE the SDK's permission callback (verified, not reasoned).

    There is no `require_safe_hook_script` twin on purpose. The sole consumer,
    `gate._run_guard_hook`, has to answer with a `(decision, reason)` tuple rather than
    raise, so a raising wrapper would be code nothing calls.
    """
    if not isinstance(script, str):
        return False
    return bool(HOOK_SCRIPT_RE.fullmatch(script)) and ".." not in script


def require_safe_log_key(key: object) -> None:
    """As above, but also admits the daemon's own streams — see this module's docstring."""
    # `isinstance` FIRST: a bare `key in RESERVED_LOG_KEYS` raises TypeError on an
    # unhashable value, and control-socket JSON can deliver a list or a dict here — which
    # would defeat the point of `is_safe_worker_name` being total over its input type.
    if isinstance(key, str) and key in RESERVED_LOG_KEYS:
        return
    if not is_safe_worker_name(key):
        raise ValueError(
            f"invalid event log key {key!r}: must match {WORKER_NAME_RE.pattern} "
            f"(or be one of the daemon's own streams {sorted(RESERVED_LOG_KEYS)}) "
            "and contain no '..' (the key is used as a filename)"
        )
