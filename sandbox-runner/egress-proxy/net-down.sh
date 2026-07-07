#!/usr/bin/env bash
# ECA-64 egress topology teardown — remove the proxy sidecar + both networks.
# Idempotent; safe to call even if net-up.sh never ran.
set -euo pipefail

NET_INTERNAL="${ECA_NET_INTERNAL:-eca-egress-internal}"
NET_EXTERNAL="${ECA_NET_EXTERNAL:-eca-egress-external}"
PROXY_NAME="${ECA_PROXY_NAME:-eca-egress-proxy}"

log() { printf '[net-down] %s\n' "$*" >&2; }

docker rm -f "$PROXY_NAME" >/dev/null 2>&1 || true
docker network rm "$NET_INTERNAL" >/dev/null 2>&1 || true
docker network rm "$NET_EXTERNAL" >/dev/null 2>&1 || true
log "down"
