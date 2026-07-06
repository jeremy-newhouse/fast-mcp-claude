# egress-proxy — filtering egress sidecar (ECA-64, AC#1 egress clause)

A default-**deny** forward proxy (tinyproxy) that is the agent container's **only**
route to the network. The agent sits on an `--internal` docker network with no
gateway; the proxy straddles that internal net and a normal uplink net, and only
lets through hosts matching `allowlist.conf` (GitHub + Bedrock + DNS).

## Why a proxy (not iptables)

On Docker Desktop macOS there is **no host-side iptables** reachable, and the
agent runs `--cap-drop=ALL` so it cannot firewall itself. A proxy sidecar on an
internal network is therefore the day-one egress control on Mac peers
(`container-sandbox.md`). On Linux/ECS the same allowlist can additionally be
enforced with real iptables/ipset; the proxy remains valid there too.

## Ownership (Q2)

- **ECA-64 (this dir)** owns the *artifact*: the proxy image, `allowlist.conf`,
  and the network topology (`net-up.sh` / `net-down.sh`).
- **ECA-65 (spawner)** owns the *runtime wiring*: at launch it stands up the
  topology and runs the agent with `--network eca-egress-internal` +
  `-e HTTPS_PROXY=http://eca-egress-proxy:8888`. The smoke test (`../smoke/`)
  stands the topology up itself so AC#5 does not block on ECA-65 existing.

## Topology

```
  [agent container]            eca-egress-internal  (--internal: no route out)
      |  HTTPS_PROXY=http://eca-egress-proxy:8888
      v
  [eca-egress-proxy] ── eca-egress-external (bridge, uplink) ──> GitHub / Bedrock
```

`net-up.sh` builds the image, creates both networks (idempotent), starts the
proxy on the external net, and attaches it to the internal net. `net-down.sh`
tears it all down. Both honor overrides via env (`ECA_NET_INTERNAL`,
`ECA_PROXY_NAME`, `ECA_PROXY_IMAGE`, `ECA_PROXY_BUILD`, …).

```bash
./net-up.sh          # -> prints ECA_PROXY_URL=http://eca-egress-proxy:8888
# ... run the agent on --network eca-egress-internal with HTTPS_PROXY set ...
./net-down.sh
```

## Allowlist

`allowlist.conf` is anchored extended-regex, matched case-insensitively against
the destination host: `github.com`, `api.github.com`, `codeload.github.com`,
`*.githubusercontent.com`, `github-cloud.s3.amazonaws.com`, `uploads.github.com`,
and `bedrock[-runtime].<region>.amazonaws.com`. Anchoring (`^…$`) blocks
look-alikes like `notgithub.com`. Anything unmatched is refused
(`FilterDefaultDeny Yes`).

## Residuals / honest limits

- **HTTPS is host-filtered, not path-filtered.** All our traffic is HTTPS, and a
  `CONNECT` tunnel only exposes the destination host (TLS encrypts the path). We
  deliberately do **not** MITM/`ssl_bump`, so filtering is by host. Path-level
  filtering would require terminating TLS in the proxy — out of scope and its own
  risk. The `tinyproxy.conf` comments state this.
- **The allowlist is a speed-bump, not an exfil boundary (design finding 15).**
  Data-plane exfiltration through the two *allowed* channels is possible by
  construction: `git push` to a write-scoped repo, or encoding data into Bedrock
  request bodies. The real mitigation is the operator's **fine-grained, short-TTL**
  GitHub token + no extra in-container secrets — not the network filter.
- **iptables egress** (the stronger Linux control) is not reachable on Docker
  Desktop; the proxy substitutes on Mac. Linux/ECS may stack both.
