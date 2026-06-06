#!/usr/bin/env bash
set -euo pipefail

# Launcher sidecar: turns this machine into a spawn target for the eCA fleet.
# Strict opt-in — set LAUNCHER_ENABLED=true (and a launcher_cwd_allowlist /
# launcher_tools_ceiling) in .env before this does any work; otherwise it idles.
# Needs the local fast-mcp-claude server running (./start.sh) and the claude CLI
# on PATH.

APP_NAME="fast-mcp-claude-launcher"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

pm2 start "uv run fast-mcp-claude-launcher" \
  --name "$APP_NAME" \
  --cwd "$SCRIPT_DIR" \
  --log "$SCRIPT_DIR/logs/launcher.log" \
  --time \
  --merge-logs

echo "Started $APP_NAME with PM2"
echo "  pm2 logs $APP_NAME    # view logs"
echo "  pm2 stop $APP_NAME    # stop"
echo "  pm2 restart $APP_NAME # restart"
echo "  pm2 delete $APP_NAME  # remove"
