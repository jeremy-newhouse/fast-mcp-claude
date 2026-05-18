#!/usr/bin/env bash
set -euo pipefail

APP_NAME="fast-mcp-claude"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

export MCP_HOST="${MCP_HOST:-0.0.0.0}"

pm2 start "uv run fast-mcp-claude" \
  --name "$APP_NAME" \
  --cwd "$SCRIPT_DIR" \
  --log "$SCRIPT_DIR/logs/mcp-server.log" \
  --time \
  --merge-logs

echo "Started $APP_NAME with PM2"
echo "  pm2 logs $APP_NAME    # view logs"
echo "  pm2 stop $APP_NAME    # stop"
echo "  pm2 restart $APP_NAME # restart"
echo "  pm2 delete $APP_NAME  # remove"
