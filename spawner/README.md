# spawner (ECA-65)

The **per-peer spawner**: the sole NATS client on the operator's peer, and the
only launcher of the ECA-64 hardened agent-sandbox container. Sibling of
`worker-supervisor/` and `sandbox-runner/`, deliberately narrow in scope — it
owns exactly one job: pull a dispatched job off the bus, run it in a hardened
container, and publish the result/events back. Design of record (in the
`evolv-coder-agent` repo — do not redesign here): the ECA-63 coordination-bus
contract and ECA-65's own dev plan.

## What it does

`spawner` binds a durable NATS pull consumer (`spawner-<member>-<machine>`) on
the `dispatch.<member>.<machine>` subject, filtered work-queue style so one
consumer owns one machine's jobs. On boot, **before** pulling any new work, it
reconciles every local non-terminal job record against container reality
(alive → attach and wait it out; dead/absent → relaunch) — this reconciliation
pass *is* the restart-recovery story, not an afterthought.

Per job it decides, from the local job-state CAS table, whether to `launch`
(no local record), `attach` (already running), or `relaunch` (redelivered,
container gone) — then either way it launches the hardened container per the
`sandbox-runner/` contract: `--network <egress-internal>` with an HTTPS proxy,
`--cap-drop=ALL --security-opt no-new-privileges` + seccomp, read-only rootfs,
and secrets as `0400` bind-mounts (never `--env-file`/image layers/`-e`). It
never mints tokens itself — it reads the member's own token from the peer's
local secret store.

While the container runs, it tails `events.jsonl` (append+fsync'd live) and
republishes each line to `jobs.<member>.<job_id>.event`; after exit it reads
`result.json` (written last, via atomic rename) once and publishes
`jobs.<member>.<job_id>.result`. Inline payloads are capped below the 8 MB
NATS `max_payload`; oversize bodies truncate with an explicit marker rather
than being silently dropped. A `PresenceHeartbeat` writes liveness to a
`presence.<member>.<machine>` KV key every 10s (readers treat `age > 30s` as
offline).

## Config

`SPAWNER_`-prefixed env vars / `.env` (see `Settings` in
`src/spawner/config.py`): `NATS_URL`, `MEMBER_ID`, `MACHINE_ID` (subject
segments, validated against the hub's own grammar), `AGENT_IMAGE`,
`DOCKER_BIN`, `EGRESS_NETWORK`, `EGRESS_PROXY_NAME`, `SECCOMP_PATH`,
`JOB_ROOT`, `GH_TOKEN_PATH` / `BEDROCK_SECRET_PATH` (secret file paths, not
values), `DB_PATH` (peer-local job-state sqlite — separate from
`fast-mcp-claude`'s own store), `PRESENCE_INTERVAL_S`, and the inline-payload
caps. `AGENT_REPLAY=true` runs the sandbox in a cred-free canned-reply mode
for exercising the pipeline before real model credentials are wired up.

## Run & test

```sh
uv venv && uv sync
uv run spawner                 # long-running; pm2-managed in production
uv run ruff check .
uv run pytest                  # unit tests are fake-backed (FakeMsg/FakeJs/FakeProcessor/...)
uv run pytest tests/test_integration_nats.py   # live JetStream round-trip; skips without a nats-server binary
```

## Layout

```
src/spawner/
  app.py           # boot: connect -> bind consumer -> reconcile -> pull loop + presence
  config.py        # Settings (SPAWNER_* env)
  consumer.py      # durable pull consumer + pull loop (thin plumbing)
  processor.py     # per-job (re)delivery decision table: launch/attach/relaunch
  launcher.py      # hardened `docker run` invocation (the only launcher)
  relay.py         # job-dir tail -> events/result publish, with the inline-payload cap policy
  presence.py       # JsPublisher + PresenceHeartbeat
  store.py         # peer-local job-state CAS (sqlite)
  bus_contract.py  # vendored copy of the hub's stream/consumer/subject constants
```

## Residuals

The vendored `bus_contract.py` is a manually-kept copy of the hub's
declarations (a cross-repo packaging dependency was rejected) — the live
`test_integration_nats.py` round-trip is the drift backstop, not a promise
the copy can't go stale between test runs.
