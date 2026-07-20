#!/usr/bin/env bash
#
# start-session.sh — launch an interactive Claude Code dev session that is
# "fleet-visible" to the eCA brain (Phase 4, live-session legs).
#
# It wires up-reporting hooks (SessionStart/UserPromptSubmit/Stop -> fast-mcp-claude-session-hook
# that write a local status file) and ONE of two down-delivery mechanisms, then execs claude:
#
#   notify+pull (default):
#     - the fast-mcp-claude-session SIDECAR (background) — sole announcer of this session's
#       presence (role="live-session", identity "<peer>.<repo>") + an inbox watcher that fires
#       a macOS notification when the brain sends a prompt;
#     - the local mesh server as the "claude-local" MCP server, so /fleet-inbox can pull a
#       parked prompt (wait_for_instruction) and answer it (reply).
#
#   channel push (CHANNEL_MODE=1, or CHANNEL_ENABLED=true in .env):
#     - the fast-mcp-claude-channel SIDECAR — spawned by claude via .mcp.json +
#       --dangerously-load-development-channels — is the sole announcer (announce(channel:true))
#       AND auto-injects each brain-sent prompt as a <channel> turn, routing the reply back over
#       the mesh (no /fleet-inbox). Tool calls relay through it for approval. The session runs in
#       --permission-mode default so consequential tools gate (the relay needs an open dialog).
#       Channels are a research preview (claude.ai auth required — peers have it; the brain is
#       Bedrock and only POSTs). Proven live on CC 2.1.168. See docs/channels (and ADR-0010
#       in the evolv-coder-agent repo).
#
# Run this from inside the repo you want to work in:
#     /path/to/fast-mcp-claude/start-session.sh
# Optional overrides via env: PEER_NAME, MCP_API_KEY, MCP_LOCAL_URL, FLEET_IDENTITY, CHANNEL_MODE.
set -euo pipefail

# Resolve the REAL location of this script, following symlinks, so it works when invoked via a
# symlink on PATH (e.g. ~/.local/bin/start-session -> this file). A naive dirname of BASH_SOURCE
# would resolve to the symlink's dir, not the repo, breaking the .env + .venv/bin lookups below.
# (BSD readlink on macOS has no -f, so walk the link chain manually; handles relative links.)
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in /*) ;; *) SOURCE="$DIR/$SOURCE" ;; esac
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
FMC_REPO="$SCRIPT_DIR"

# --- load PEER_NAME / MCP_API_KEY from the repo .env if not already in the env ----------
# Trailing `|| true`: under `set -euo pipefail`, a no-match grep exits 1 and pipefail would
# abort the whole script inside the `${VAR:-$(_envget KEY)}` substitution. That bites any key
# absent from .env — notably CHANNEL_ENABLED (never a standard key), which would break the
# DEFAULT notify+pull path for everyone. Swallow the no-match so a missing key just yields "".
_envget() { grep -E "^$1=" "$FMC_REPO/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'" || true; }
PEER_NAME="${PEER_NAME:-$(_envget PEER_NAME)}"; PEER_NAME="${PEER_NAME:-local}"
# Mesh bearer is authoritative from THIS repo's .env (the local mesh is configured from it).
# Prefer .env over any inherited MCP_API_KEY (e.g. a shared ~/.zshrc that exports ANOTHER
# machine's mesh key) which the local mesh would reject with 401, leaving the channel/session
# sidecar unable to announce. Fall back to the inherited env only when .env doesn't define it.
_ENV_MCP_KEY="$(_envget MCP_API_KEY)"
MCP_API_KEY="${_ENV_MCP_KEY:-${MCP_API_KEY:-}}"
MCP_PORT="${MCP_PORT:-$(_envget MCP_PORT)}"; MCP_PORT="${MCP_PORT:-5473}"
MCP_LOCAL_URL="${MCP_LOCAL_URL:-http://127.0.0.1:${MCP_PORT}/mcp}"

# --- channel mode: auto-PUSH down-delivery (vs the default notify+pull) -------------------
# When on, a fast-mcp-claude-channel sidecar (spawned by claude via .mcp.json + the
# --dangerously-load-development-channels flag) injects each brain-sent prompt straight into
# this session as a <channel> turn and routes the reply back over the mesh — no /fleet-inbox.
# It is then the SOLE presence announcer (channel:true), so we do NOT also start session.py.
# Gate from CHANNEL_MODE env, else the repo .env CHANNEL_ENABLED; default OFF (notify+pull).
CHANNEL_MODE="${CHANNEL_MODE:-$(_envget CHANNEL_ENABLED)}"
case "$CHANNEL_MODE" in
  1|true|TRUE|True|yes|YES|on|ON) CHANNEL_MODE=1 ;;
  *) CHANNEL_MODE=0 ;;
esac

# --- resolve identity: <peer>.<repo-slug> (mesh SESSION_RE: [A-Za-z0-9_.-]) --------------
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
REPO_BASE="$(basename "$REPO_ROOT")"
REPO_SLUG="$(printf '%s' "$REPO_BASE" | tr -c 'A-Za-z0-9_.-' '-' )"
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
# Short stable hash of the ABSOLUTE repo root — the fallback distinguisher when there is
# neither a session name NOR a branch (detached HEAD), so two same-basename checkouts still
# get DISTINCT mailboxes instead of clobbering one shared presence row + inbox.
REPO_HASH="$(printf '%s' "$REPO_ROOT" | cksum | cut -d' ' -f1)"

# Session NAME (ADR-0016): distinguishes multiple sessions in the SAME repo on one host and
# gives each a human handle. Defaults to the git branch; override with SESSION_NAME. Slugged to
# the mesh SESSION_RE and folded into the identity as its final segment (<peer>.<repo>.<name>),
# so same-dir sessions get DISTINCT presence rows + are addressable by name. Falls back to the
# path-hash suffix only when there is neither a name nor a branch (detached HEAD).
SESSION_NAME="${SESSION_NAME:-}"
if [ -z "$SESSION_NAME" ] && [ "$BRANCH" != "?" ]; then SESSION_NAME="$BRANCH"; fi
NAME_SLUG="$(printf '%s' "$SESSION_NAME" | tr -c 'A-Za-z0-9_.-' '-' | sed 's/^[-.]*//; s/[-.]*$//')"

# Session DESCRIPTION (ECA-23): free-text purpose, operator-set only (no default, unlike
# SESSION_NAME) — published in presence so the brain/operator can tell sessions apart by what
# they're working on, not just name/branch. Not slugged: it flows through the status file/
# presence metadata as-is, never into the mesh identity.
SESSION_DESCRIPTION="${SESSION_DESCRIPTION:-}"
if [ -n "$NAME_SLUG" ]; then
  IDENTITY="${FLEET_IDENTITY:-${PEER_NAME}.${REPO_SLUG}.${NAME_SLUG}}"
else
  IDENTITY="${FLEET_IDENTITY:-${PEER_NAME}.${REPO_SLUG}-${REPO_HASH}}"
fi

# Hard-fail on an identity the mesh server would reject (SESSION_RE ^[A-Za-z0-9_.-]{1,128}$),
# mirroring validate_launcher_identity. A stray space/quote/unicode in PEER_NAME or
# FLEET_IDENTITY would otherwise (a) make announce() silently fail every heartbeat so the
# session never appears in who(), and (b) break the single-quoting of the hook command below.
case "$IDENTITY" in
  ""|*[!A-Za-z0-9_.-]*)
    echo "ERROR: identity '$IDENTITY' must match ^[A-Za-z0-9_.-]{1,128}\$ (no spaces/quotes/unicode/slashes)." >&2
    echo "       Fix PEER_NAME in $FMC_REPO/.env, or pass FLEET_IDENTITY=<valid-id>." >&2
    exit 2 ;;
esac
if [ "${#IDENTITY}" -gt 128 ]; then
  echo "ERROR: identity '$IDENTITY' exceeds the mesh's 128-char limit." >&2; exit 2
fi

# --- resolve the new console scripts (prefer the repo venv, else PATH) -------------------
resolve_bin() {
  if [ -x "$FMC_REPO/.venv/bin/$1" ]; then echo "$FMC_REPO/.venv/bin/$1";
  elif command -v "$1" >/dev/null 2>&1; then command -v "$1";
  else echo ""; fi
}
BIN_SESSION="$(resolve_bin fast-mcp-claude-session)"
BIN_HOOK="$(resolve_bin fast-mcp-claude-session-hook)"
BIN_CHANNEL="$(resolve_bin fast-mcp-claude-channel)"
if [ -z "$BIN_SESSION" ] || [ -z "$BIN_HOOK" ]; then
  echo "ERROR: fast-mcp-claude-session(-hook) not found. Run 'uv sync' in $FMC_REPO." >&2
  exit 2
fi
if [ "$CHANNEL_MODE" = "1" ] && [ -z "$BIN_CHANNEL" ]; then
  echo "ERROR: CHANNEL_MODE=1 but fast-mcp-claude-channel not found. Run 'uv sync' in $FMC_REPO." >&2
  exit 2
fi

# --- paths: status file (hooks write, sidecar reads) + unread badge ----------------------
SESS_DIR="$HOME/.fast-mcp-claude/sessions"
mkdir -p "$SESS_DIR"
STATUS_FILE="$SESS_DIR/$IDENTITY.json"
BADGE_FILE="$SESS_DIR/$IDENTITY.badge"

# seed the status file (valid JSON via python so the hook/sidecar merge cleanly)
python3 - "$STATUS_FILE" "$IDENTITY" "$PEER_NAME" "$REPO_BASE" "$REPO_ROOT" "$BRANCH" "$NAME_SLUG" "$SESSION_DESCRIPTION" <<'PY'
import json, sys, time
path, identity, machine, repo, cwd, branch, name, description = sys.argv[1:9]
json.dump({"identity": identity, "machine": machine, "repo": repo, "cwd": cwd,
           "branch": branch, "name": name or None, "session_description": description or None,
           "status": "starting", "started_at": time.time(), "updated_at": time.time(),
           "last": ""}, open(path, "w"))
PY

# --- ensure the /fleet-inbox pull command is installed at user level ---------------------
# Only in notify+pull mode: channel mode auto-delivers and omits the claude-local server, so the
# /fleet-inbox command (which calls mcp__claude-local__*) would be present-but-broken there.
if [ "$CHANNEL_MODE" != "1" ]; then
  mkdir -p "$HOME/.claude/commands"
  if [ -f "$FMC_REPO/.claude/commands/fleet-inbox.md" ]; then
    cp -f "$FMC_REPO/.claude/commands/fleet-inbox.md" "$HOME/.claude/commands/fleet-inbox.md"
  fi
fi

# --- temp MCP config + hook settings, both auto-cleaned ----------------------------------
TMPDIR_RUN="$(mktemp -d "${TMPDIR:-/tmp}/eca-session.XXXXXX")"
cleanup() { rm -rf "$TMPDIR_RUN"; }
trap cleanup EXIT
MCPCFG="$TMPDIR_RUN/mcp.json"
SETTINGS="$TMPDIR_RUN/settings.json"
umask 077

# The bearer goes via ENV, never argv: argv is world-readable via `ps` (even cross-uid),
# so passing it as an argument would leak the mesh credential. (Mirrors how the launcher
# keeps the bearer out of worker argv.) The output mcp.json stays 0600 (umask 077 above).
#
# Channel mode: the inner agent's ONLY mcp server is the channel sidecar, which owns the
# mesh worker verbs (wait_for_instruction/reply) + presence + the permission relay. The agent
# gets NO claude-local (no raw mesh verbs — invariant 9); its only reply path is the channel's
# own `reply` tool. The channel config (identity/url/bearer/status-file) rides in the .mcp.json
# `env` block (passed to the subprocess env, NOT argv).
# notify+pull mode: the agent gets claude-local so /fleet-inbox can wait_for_instruction+reply.
MCP_API_KEY="${MCP_API_KEY:-}" python3 - \
  "$MCPCFG" "$MCP_LOCAL_URL" "$CHANNEL_MODE" "$BIN_CHANNEL" "$IDENTITY" "$STATUS_FILE" <<'PY'
import json, os, sys
path, url, channel_mode, channel_bin, identity, status_file = sys.argv[1:7]
key = os.environ.get("MCP_API_KEY")
if channel_mode == "1":
    env = {
        "CHANNEL_ENABLED": "true",
        "CRM_IDENTITY": identity,
        "CRM_LOCAL_URL": url,
        "CRM_SESSION_STATUS_FILE": status_file,
    }
    if key:
        env["MCP_API_KEY"] = key
    servers = {"fast-mcp-claude-channel": {"command": channel_bin, "env": env}}
else:
    srv = {"type": "http", "url": url}
    if key:
        srv["headers"] = {"Authorization": f"Bearer {key}"}
    servers = {"claude-local": srv}
json.dump({"mcpServers": servers}, open(path, "w"))
PY

# up-reporting hooks: each invokes the status-writer with the status-file path in env.
HOOK_CMD="CRM_SESSION_STATUS_FILE='$STATUS_FILE' '$BIN_HOOK'"
python3 - "$SETTINGS" "$HOOK_CMD" <<'PY'
import json, sys
path, cmd = sys.argv[1:3]
entry = [{"matcher": "", "hooks": [{"type": "command", "command": cmd}]}]
json.dump({"hooks": {"SessionStart": entry, "UserPromptSubmit": entry, "Stop": entry}},
          open(path, "w"))
PY

echo "eCA live session: identity=$IDENTITY  repo=$REPO_BASE@$BRANCH  peer=$PEER_NAME" >&2

# --- become claude, with the right down-delivery mechanism -------------------------------
# After `exec claude`, this shell's pid is reused by claude, so a backgrounded sidecar's
# parent pid stays valid and it exits when the session does.
if [ "$CHANNEL_MODE" = "1" ]; then
  # PUSH: the fast-mcp-claude-channel sidecar is spawned BY claude (the .mcp.json entry above)
  # and is the SOLE presence announcer (announce(channel:true)); we do NOT also start
  # session.py (two announcers on one identity clobber each other's presence). Down-delivery
  # is automatic — no /fleet-inbox. The session runs in --permission-mode default (NOT auto /
  # NOT skip): consequential tools in a channel turn must open a dialog so the sidecar's
  # permission relay can gate them (admin-triggered -> auto-allow; non-admin -> Teams approval).
  echo "  status: $STATUS_FILE" >&2
  echo "  channel mode: AUTO-PUSH on — the channel sidecar announces + delivers (no /fleet-inbox)" >&2
  echo "  one-time per repo: accept the folder-trust + dev-channels prompts; then pm2/tmux is unattended" >&2
  exec claude --mcp-config "$MCPCFG" --settings "$SETTINGS" \
    --permission-mode default \
    --dangerously-load-development-channels "server:fast-mcp-claude-channel" "$@"
else
  # NOTIFY+PULL: session.py is the sole announcer + inbox notifier; the operator pulls with
  # /fleet-inbox (wait_for_instruction -> reply via claude-local).
  echo "  status: $STATUS_FILE   inbox badge: $BADGE_FILE" >&2
  echo "  the brain can now reach this session; on a push run: /fleet-inbox $IDENTITY" >&2
  MCP_API_KEY="$MCP_API_KEY" CRM_IDENTITY="$IDENTITY" \
    "$BIN_SESSION" --identity "$IDENTITY" --enabled \
      --local-url "$MCP_LOCAL_URL" --status-file "$STATUS_FILE" \
      --badge-file "$BADGE_FILE" --parent-pid "$$" &
  exec claude --mcp-config "$MCPCFG" --settings "$SETTINGS" "$@"
fi
