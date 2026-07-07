# NOTE: no `# syntax=` frontend pin — stock instructions only, so Docker's
# builtin BuildKit frontend handles it and we skip the Docker Hub round-trip to
# resolve docker/dockerfile:1 that transiently hangs (DeadlineExceeded, ECA-64).
# ECA-64 egress filtering proxy (AC#1 egress clause).
#
# A tiny default-DENY forward proxy (tinyproxy). The agent container sits on an
# INTERNAL docker network with no route to the outside; its only egress is this
# sidecar, which allows CONNECT/GET only to hosts matching `allowlist.conf`
# (GitHub + Bedrock + DNS). Everything else is refused.
#
# Ownership (Q2): ECA-64 ships this artifact; the spawner (ECA-65) wires it at
# launch (`net-up.sh` shows the topology). cap-drop=ALL forbids in-container
# firewalling, and Docker Desktop's VM exposes no host iptables — so a proxy
# sidecar is the macOS-day-one egress control (container-sandbox.md).
FROM alpine:3.20

RUN apk add --no-cache tinyproxy

# Config + the host allowlist (default-deny; only these regexes pass).
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY allowlist.conf /etc/tinyproxy/allowlist.conf

# Non-root; the alpine package creates the `tinyproxy` user/group.
USER tinyproxy

EXPOSE 8888

# -d: run in the foreground (no daemonize) and log to stdout so the spawner /
# smoke can observe allow/deny decisions via `docker logs`.
ENTRYPOINT ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
