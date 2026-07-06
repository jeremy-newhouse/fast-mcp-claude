#!/usr/bin/env bash
# Run the worker-supervisor daemon under pm2 (name: worker-supervisor).
set -euo pipefail

APP_NAME="worker-supervisor"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

uv sync --quiet
pm2 start "uv run worker-supervisor" \
  --name "$APP_NAME" \
  --cwd "$SCRIPT_DIR" \
  --log "$SCRIPT_DIR/logs/daemon.log" \
  --time \
  --merge-logs
pm2 save
