#!/usr/bin/env bash
#
# start-session.sh — launch an interactive Claude Code dev session that is
# "fleet-visible" to the eCA brain (Phase 4, live-session legs).
#
# It wires three things onto a normal `claude` session, then execs into it:
#   1. up-reporting hooks (SessionStart/UserPromptSubmit/Stop -> fast-mcp-claude-session-hook)
#      that write a local status file ("what am I working on");
#   2. the fast-mcp-claude-session SIDECAR (background) — the sole announcer of this
#      session's presence (role="live-session", identity "<peer>.<repo>") + an inbox
#      watcher that fires a macOS notification when the brain sends this session a prompt;
#   3. the local mesh server as the "claude-local" MCP server, so /fleet-inbox can pull
#      a parked prompt (wait_for_instruction) and answer it (reply).
#
# Channels (auto-push) are NOT used: Claude Code 2.1.x removed the dev-channel load path,
# so down-delivery is notify+pull. Run this from inside the repo you want to work in:
#     /path/to/fast-mcp-claude/start-session.sh
# Optional overrides via env: PEER_NAME, MCP_API_KEY, MCP_LOCAL_URL, FLEET_IDENTITY.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FMC_REPO="$SCRIPT_DIR"

# --- load PEER_NAME / MCP_API_KEY from the repo .env if not already in the env ----------
_envget() { grep -E "^$1=" "$FMC_REPO/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"; }
PEER_NAME="${PEER_NAME:-$(_envget PEER_NAME)}"; PEER_NAME="${PEER_NAME:-local}"
MCP_API_KEY="${MCP_API_KEY:-$(_envget MCP_API_KEY)}"
MCP_PORT="${MCP_PORT:-$(_envget MCP_PORT)}"; MCP_PORT="${MCP_PORT:-5473}"
MCP_LOCAL_URL="${MCP_LOCAL_URL:-http://127.0.0.1:${MCP_PORT}/mcp}"

# --- resolve identity: <peer>.<repo-slug> (mesh SESSION_RE: [A-Za-z0-9_.-]) --------------
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
REPO_BASE="$(basename "$REPO_ROOT")"
REPO_SLUG="$(printf '%s' "$REPO_BASE" | tr -c 'A-Za-z0-9_.-' '-' )"
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
# Short stable hash of the ABSOLUTE repo root so two same-basename checkouts (worktrees,
# ~/a/api and ~/b/api) get DISTINCT mailboxes instead of clobbering one shared presence
# row + inbox. The human still addresses the session by machine/repo (metadata.repo).
REPO_HASH="$(printf '%s' "$REPO_ROOT" | cksum | cut -d' ' -f1)"
IDENTITY="${FLEET_IDENTITY:-${PEER_NAME}.${REPO_SLUG}-${REPO_HASH}}"

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
if [ -z "$BIN_SESSION" ] || [ -z "$BIN_HOOK" ]; then
  echo "ERROR: fast-mcp-claude-session(-hook) not found. Run 'uv sync' in $FMC_REPO." >&2
  exit 2
fi

# --- paths: status file (hooks write, sidecar reads) + unread badge ----------------------
SESS_DIR="$HOME/.fast-mcp-claude/sessions"
mkdir -p "$SESS_DIR"
STATUS_FILE="$SESS_DIR/$IDENTITY.json"
BADGE_FILE="$SESS_DIR/$IDENTITY.badge"

# seed the status file (valid JSON via python so the hook/sidecar merge cleanly)
python3 - "$STATUS_FILE" "$IDENTITY" "$PEER_NAME" "$REPO_BASE" "$REPO_ROOT" "$BRANCH" <<'PY'
import json, sys, time
path, identity, machine, repo, cwd, branch = sys.argv[1:7]
json.dump({"identity": identity, "machine": machine, "repo": repo, "cwd": cwd,
           "branch": branch, "status": "starting", "started_at": time.time(),
           "updated_at": time.time(), "last": ""}, open(path, "w"))
PY

# --- ensure the /fleet-inbox pull command is installed at user level ---------------------
mkdir -p "$HOME/.claude/commands"
if [ -f "$FMC_REPO/.claude/commands/fleet-inbox.md" ]; then
  cp -f "$FMC_REPO/.claude/commands/fleet-inbox.md" "$HOME/.claude/commands/fleet-inbox.md"
fi

# --- temp MCP config (claude-local) + hook settings, both auto-cleaned -------------------
TMPDIR_RUN="$(mktemp -d "${TMPDIR:-/tmp}/eca-session.XXXXXX")"
cleanup() { rm -rf "$TMPDIR_RUN"; }
trap cleanup EXIT
MCPCFG="$TMPDIR_RUN/mcp.json"
SETTINGS="$TMPDIR_RUN/settings.json"
umask 077

# The bearer goes via ENV, never argv: argv is world-readable via `ps` (even cross-uid),
# so passing it as an argument would leak the mesh credential. (Mirrors how the launcher
# keeps the bearer out of worker argv.) The output mcp.json stays 0600 (umask 077 above).
MCP_API_KEY="${MCP_API_KEY:-}" python3 - "$MCPCFG" "$MCP_LOCAL_URL" <<'PY'
import json, os, sys
path, url = sys.argv[1:3]
key = os.environ.get("MCP_API_KEY")
srv = {"type": "http", "url": url}
if key:
    srv["headers"] = {"Authorization": f"Bearer {key}"}
json.dump({"mcpServers": {"claude-local": srv}}, open(path, "w"))
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
echo "  status: $STATUS_FILE   inbox badge: $BADGE_FILE" >&2
echo "  the brain can now reach this session; on a push run: /fleet-inbox $IDENTITY" >&2

# --- start the sidecar (background), tied to THIS process; then become claude ------------
# After `exec claude`, this shell's pid is reused by claude, so the sidecar's parent pid
# stays valid and it exits when the session does.
MCP_API_KEY="$MCP_API_KEY" CRM_IDENTITY="$IDENTITY" \
  "$BIN_SESSION" --identity "$IDENTITY" --enabled \
    --local-url "$MCP_LOCAL_URL" --status-file "$STATUS_FILE" \
    --badge-file "$BADGE_FILE" --parent-pid "$$" &

exec claude --mcp-config "$MCPCFG" --settings "$SETTINGS" "$@"
