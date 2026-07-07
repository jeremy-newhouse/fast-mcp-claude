#!/usr/bin/env bash
# ECA-64 egress topology — stand up the internal no-egress network + proxy sidecar.
#
# This is the artifact ECA-64 owns; the spawner (ECA-65) will call the equivalent
# at launch. It is idempotent so the smoke (and ad-hoc runs) can call it freely.
#
# Topology:
#
#     [agent container]        (INTERNAL net: no default route out)
#         |  HTTPS_PROXY=http://$PROXY_NAME:8888
#         v
#     [egress-proxy] --- (EXTERNAL bridge net: the only uplink) ---> internet
#
# The agent joins ONLY $NET_INTERNAL (created with --internal, so docker installs
# no gateway/NAT for it). The proxy straddles both nets, so it is the agent's sole
# path to GitHub/Bedrock, filtered by allowlist.conf.
set -euo pipefail

NET_INTERNAL="${ECA_NET_INTERNAL:-eca-egress-internal}"
NET_EXTERNAL="${ECA_NET_EXTERNAL:-eca-egress-external}"
PROXY_NAME="${ECA_PROXY_NAME:-eca-egress-proxy}"
PROXY_IMAGE="${ECA_PROXY_IMAGE:-eca/egress-proxy:dev}"
BUILD="${ECA_PROXY_BUILD:-1}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '[net-up] %s\n' "$*" >&2; }

if [ "$BUILD" = "1" ]; then
  log "building $PROXY_IMAGE"
  docker build -q -t "$PROXY_IMAGE" "$here" >/dev/null
fi

# Internal (no-egress) network shared with the agent.
if ! docker network inspect "$NET_INTERNAL" >/dev/null 2>&1; then
  log "creating internal network $NET_INTERNAL (--internal)"
  docker network create --internal "$NET_INTERNAL" >/dev/null
fi

# External (uplink) network — the proxy's only route out.
if ! docker network inspect "$NET_EXTERNAL" >/dev/null 2>&1; then
  log "creating external network $NET_EXTERNAL"
  docker network create "$NET_EXTERNAL" >/dev/null
fi

# (Re)start the proxy sidecar on the external net, then attach the internal net.
docker rm -f "$PROXY_NAME" >/dev/null 2>&1 || true
log "starting proxy $PROXY_NAME"
docker run -d --rm \
  --name "$PROXY_NAME" \
  --network "$NET_EXTERNAL" \
  --cap-drop=ALL \
  --security-opt no-new-privileges \
  "$PROXY_IMAGE" >/dev/null
docker network connect "$NET_INTERNAL" "$PROXY_NAME"

log "up: agent -> --network $NET_INTERNAL, HTTPS_PROXY=http://$PROXY_NAME:8888"
# Emit the key facts on stdout for callers that want to eval/capture them.
echo "ECA_NET_INTERNAL=$NET_INTERNAL"
echo "ECA_NET_EXTERNAL=$NET_EXTERNAL"
echo "ECA_PROXY_NAME=$PROXY_NAME"
echo "ECA_PROXY_URL=http://$PROXY_NAME:8888"
