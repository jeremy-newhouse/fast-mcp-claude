#!/bin/sh
# ECA-64 entrypoint (AC#3): bridge the Bedrock bearer from a 0400 bind-mounted
# secret file into THIS process's environment ONLY, then exec the runner.
#
# Why a file, not `-e`/`--env-file`: a container-wide env var shows in
# `docker inspect`, leaks to every child, and lands in logs. Reading the file
# here and exporting into the exec'd runner's env keeps the bearer off the
# container config and out of unrelated subprocesses (CLAUDE_CODE_* scrubbing
# plus disallowed WebFetch/WebSearch keep it out of tool subprocesses too).
#
# The GitHub token is NOT handled here — gitcreds.py reads it lazily via git's
# credential helper at clone time, so it never transits this shell's env.
set -eu

BEDROCK_SECRET="${BEDROCK_SECRET_PATH:-/run/secrets/bedrock}"

if [ -f "$BEDROCK_SECRET" ]; then
    # Strip trailing newline; export scoped to the exec'd process tree.
    AWS_BEARER_TOKEN_BEDROCK="$(cat "$BEDROCK_SECRET")"
    export AWS_BEARER_TOKEN_BEDROCK
fi

# Hand off to the runner as the current (uid 1000) user. `exec` makes the runner
# PID 1's direct successor so SIGTERM/SIGKILL from a wall-clock breach or the
# spawner reaches it (and its CLI subprocess group) cleanly.
exec python -m sandbox_runner "$@"
