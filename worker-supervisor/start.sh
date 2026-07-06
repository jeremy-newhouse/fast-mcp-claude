#!/usr/bin/env bash
# Run the worker-supervisor daemon under pm2 (name: worker-supervisor).
set -euo pipefail
cd "$(dirname "$0")"

uv sync --quiet
pm2 start --name worker-supervisor --interpreter none -- uv run worker-supervisor
pm2 save
