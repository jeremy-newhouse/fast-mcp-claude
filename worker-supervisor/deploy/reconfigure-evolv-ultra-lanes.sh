#!/usr/bin/env bash
# Reproducible evolv-ultra worker-supervisor lane (re)configuration — ECA-100 / ECA-99.
#
# Runs ON mbpm2 (host of the evolv-ultra lanes + their supervisor daemon). Idempotent per
# lane: kill (if live) -> remove (frees the PK name) -> spawn with the per-lane policy below.
# Safe to re-run; safe to run for a subset of lanes (pilot one, then the rest).
#
#   ./reconfigure-evolv-ultra-lanes.sh                 # all 6 lanes
#   ./reconfigure-evolv-ultra-lanes.sh ultra1          # just ultra1 (pilot)
#   ./reconfigure-evolv-ultra-lanes.sh ultra2 ultra3 ultra4 ultra5 ultra6
#
# Per-lane policy (ECA-100 AC):
#   model:  ultra1-4=claude-sonnet-5  ultra5=claude-opus-4-8  ultra6=claude-fable-5
#   budget: uncapped (--budget 1e6; the daemon SUPERVISOR_MAX_BUDGET_USD_PER_EPOCH is also
#           set high on mbpm2 so context_pct is the sole binding cycle constraint — ECA-99 #5)
#   cwd:    ~/worker-repos/<lane>/evolv-ultra  (the REPO ROOT, so setting_sources=["project"]
#           discovers the on-disk /pr-review skill; be/fe are siblings at ../ per the skill's
#           docker-compose bind-mount model. The old workspace-root cwd is why lanes could not
#           invoke /pr-review and improvised via Bash.)
#   tools:  Read,Write,Edit,Glob,Grep,Bash,Skill,Task + MCP name-wildcards for the granted
#           servers (Task = pr-review's subagent fan-out: backend-reviewer/security/simplifier)
#   limits: --max-turns 150 --wall-clock 3600 (pr-review ran 70 turns/~10min live)
#   mcp:    ~/.worker-supervisor/mcp-configs/evolv-ultra.json (generated fresh each run by
#           gen-evolv-ultra-mcp-config.py; 0600; creds in each server's own headers, worker
#           env stays scrubbed — envbuild A3).
#
# DEFERRED (see ECA-100): playwright (disabled per operator 2026-07-11 — flaky npx stdio and
# unused by evolv-ultra skills; browser-test is Bash/CLI-driven, chromium installed on mbpm2);
# fast-mcp-claude-channel is a per-live-session stdio sidecar, not a standalone server a
# supervisor lane can attach to; teams (:8326) direct-send is omitted on purpose — lanes report
# to ultra0 (the Teams/channel bridge) via the supervisor.
#
# greptile uses the operator API key at ~/.worker-supervisor/secrets/greptile.token (0600),
# NOT the built-in greptile plugin (its OAuth expired).
#
# bash 3.2 (macOS default) — NO associative arrays (ECA-97). case + indexed args only.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORKERS="$HOME/repos/fast-mcp-claude/worker-supervisor/.venv/bin/workers"
WORKSPACES="$HOME/worker-repos"
MCP_CONFIG="$HOME/.worker-supervisor/mcp-configs/evolv-ultra.json"
BUDGET=1000000
# pr-review is the primary workload: it fans out to subagents (Task) and ran 70 SDK
# turns / ~10min live, so lift max-turns above the 50 default and give a 1h wall-clock.
# Budget is uncapped (ECA-99), so max-turns + wall-clock + context_pct are the backstops.
MAX_TURNS=150
WALL_CLOCK=3600
TOOLS="Read,Write,Edit,Glob,Grep,Bash,Skill,Task,mcp__jira__*,mcp__confluence__*,mcp__langfuse__*,mcp__greptile__*,mcp__context7__*"

ALL_LANES="ultra1 ultra2 ultra3 ultra4 ultra5 ultra6"
LANES="${*:-$ALL_LANES}"

model_for() {
  case "$1" in
    ultra1|ultra2|ultra3|ultra4) echo "claude-sonnet-5" ;;
    ultra5) echo "claude-opus-4-8" ;;
    ultra6) echo "claude-fable-5" ;;
    *) echo "" ;;
  esac
}

# --- preflight -------------------------------------------------------------
[ -x "$WORKERS" ] || { echo "FATAL: workers CLI not found at $WORKERS" >&2; exit 1; }
"$WORKERS" ping >/dev/null || { echo "FATAL: supervisor daemon not responding" >&2; exit 1; }

echo "== generating MCP config =="
python3 "$HERE/gen-evolv-ultra-mcp-config.py"
[ -f "$MCP_CONFIG" ] || { echo "FATAL: MCP config not generated at $MCP_CONFIG" >&2; exit 1; }

# --- per-lane reconfigure --------------------------------------------------
for lane in $LANES; do
  model="$(model_for "$lane")"
  [ -n "$model" ] || { echo "SKIP $lane: no model mapping" >&2; continue; }
  cwd="$WORKSPACES/$lane/evolv-ultra"
  if [ ! -d "$cwd/.claude" ]; then
    echo "SKIP $lane: $cwd/.claude missing (provision the 3-repo workspace first)" >&2
    continue
  fi

  echo "== $lane -> model=$model cwd=$cwd =="
  # kill (tolerate 'not running'), then remove (frees the name; tolerate 'no such')
  "$WORKERS" kill "$lane"   >/dev/null 2>&1 || true
  "$WORKERS" remove "$lane" >/dev/null 2>&1 || true

  "$WORKERS" spawn "$lane" "$cwd" \
    --model "$model" \
    --budget "$BUDGET" \
    --max-turns "$MAX_TURNS" \
    --wall-clock "$WALL_CLOCK" \
    --tools "$TOOLS" \
    --mcp-config "$MCP_CONFIG"
done

echo "== done: $LANES =="
echo "verify: $WORKERS status"
