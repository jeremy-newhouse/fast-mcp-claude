# evolv-ultra lane deploy config (ECA-100 / ECA-99)

Reproducible (re)configuration of the evolv-ultra worker-supervisor lanes on **mbpm2**.
This is deployment config, not supervisor code — it lives here only because mbpm2 pulls
`fast-mcp-claude` and runs the lanes.

## What it sets

| Aspect | Value |
| --- | --- |
| Lanes | ultra1–ultra6 |
| Models | ultra1–4 = `claude-sonnet-5`, ultra5 = `claude-opus-4-8`, ultra6 = `claude-fable-5` |
| Budget | uncapped (`--budget 1000000`; daemon `SUPERVISOR_MAX_BUDGET_USD_PER_EPOCH` also high) — `context_pct` is the only binding cycle constraint (subscription billing; ECA-99 #5) |
| cwd | `~/worker-repos/<lane>/evolv-ultra` (repo root → project `/pr-review` skill loads; be/fe siblings at `../`) |
| Tools | `Read,Write,Edit,Glob,Grep,Bash,Skill` + MCP `jira, confluence, langfuse, greptile, context7, playwright` |
| MCP creds | materialised at runtime into `~/.worker-supervisor/mcp-configs/evolv-ultra.json` (0600, **not committed**); each server's creds in its own headers block — worker process env stays scrubbed (envbuild A3) |

## Run

```bash
cd ~/repos/fast-mcp-claude/worker-supervisor/deploy
./reconfigure-evolv-ultra-lanes.sh ultra1                     # pilot one lane
./reconfigure-evolv-ultra-lanes.sh ultra2 ultra3 ultra4 ultra5 ultra6   # the rest
./reconfigure-evolv-ultra-lanes.sh                            # all 6
~/repos/fast-mcp-claude/worker-supervisor/.venv/bin/workers status
```

Idempotent per lane: `kill → remove → spawn`. Requires the 3-repo workspace
(`evolv-ultra` + `-be` + `-fe`) to already exist under `~/worker-repos/<lane>/` — the
supervisor never clones. Provision new lanes by cloning those three repos (branch `dev`)
and copying `evolv-ultra-be/.env` from an existing lane before running the script.

## Prerequisites (one-time)

- Deployed supervisor code with the ECA-100/99 changes (per-lane `mcp_servers`, `remove`
  verb, lifecycle-budget exemption) — `git pull` + `pm2 restart worker-supervisor`.
- `SUPERVISOR_MAX_BUDGET_USD_PER_EPOCH` set high in `worker-supervisor/.env`.
- `~/.claude.json` project scopes contain the `langfuse` and `greptile` server defs (their
  `Authorization` headers are lifted from there); `MCP_API_KEY` in
  `~/repos/fast-mcp-claude/.env` (jira/confluence localhost bearer).

## Deferred / out of scope

- **fast-mcp-claude-channel** (listed in AC#2): a per-live-session stdio sidecar bound to a
  mesh identity — not a standalone server a supervisor lane can point at. Lanes report to
  **ultra0** (the Teams/channel orchestrator) via the supervisor, so they don't need it.
- **teams** (`:8326`) direct-send: intentionally omitted for the same reason (least-privilege;
  ultra0 is the bridge).
