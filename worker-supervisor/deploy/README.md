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
| Limits | `--max-turns 150 --wall-clock 3600` (pr-review ran 70 SDK turns / ~10 min live; these are now the runaway backstops since budget is uncapped) |
| cwd | `~/worker-repos/<lane>/evolv-ultra` (repo root → project `/pr-review` skill loads; be/fe siblings at `../`) |
| Tools | `Read,Write,Edit,Glob,Grep,Bash,Skill,Task` + MCP `jira, confluence, langfuse, context7` (`Task` = pr-review's subagent fan-out) |
| MCP creds | materialised at runtime into `~/.worker-supervisor/mcp-configs/evolv-ultra.json` (0600, **not committed**); each server's creds in its own headers block — worker process env stays scrubbed (envbuild A3) |
| MCP creds — exposure | **A3 is about env inheritance only; it is not containment.** These lanes grant bare `Bash`, and a lane runs as the same uid as the daemon, so any granted lane can read these credentials, and so can any UN-granted lane on the same box — from the daemon's per-turn config file and, durably, from `state.db`. ECA-135 took them out of the CLI's argv, which is what a stray `ps aux` used to scoop up by accident; ECA-136 then took the whole supervisor home to 0700/0600 (swept at boot), turned on `secure_delete`, reclaimed pages freed before it, and made `workers remove` checkpoint the WAL — without that last part a revoked grant stayed `grep`-able out of `state.db-wal` for the daemon's whole uptime, because `secure_delete` only zeroes the *new* page image. **Both require a `pm2 restart worker-supervisor`**: every one of those changes lives inside the daemon, so a `git pull` alone changes nothing on disk. Neither built a lane-to-lane boundary and there is none — every lane is this uid. Grant a server to a lane only if you would hand that lane the credential directly. **Backups are outside all of that:** on mbpm2 `~/.worker-supervisor` is `[Included]` in an active Time Machine destination, so cleartext policies for grants already made persist in existing snapshots regardless. |

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
  verb, lifecycle-budget exemption) **and the envbuild PATH augmentation** (worker PATH
  prepends `/usr/local/bin`, `/opt/homebrew/bin` so `docker`/brew tools resolve) —
  `git pull` + `pm2 restart worker-supervisor` (the restart is what applies the PATH fix).
- `SUPERVISOR_MAX_BUDGET_USD_PER_EPOCH` set high in `worker-supervisor/.env`.
- `MCP_API_KEY` in `~/repos/fast-mcp-claude/.env` (jira/confluence localhost bearer).
- `langfuse` server def in a `~/.claude.json` project scope (Basic pk/sk auth; the AWS-dev
  hosted MCP — wake the dev env if it 503s).

## Deferred / out of scope

- **playwright** (AC#2 "chrome/CDP"): disabled per operator (2026-07-11). Flaky as an `npx`
  stdio MCP, and no evolv-ultra skill invokes `mcp__playwright__*` — pr-review's
  `browser-test --local` drives playwright via Bash/CLI inside the docker stack (chromium
  headless-shell is installed on mbpm2), so the MCP is redundant.
- **fast-mcp-claude-channel** (AC#2): a per-live-session stdio sidecar bound to a mesh
  identity — not a standalone server a supervisor lane can point at. Lanes report to **ultra0**
  (the Teams/channel orchestrator) via the supervisor, so they don't need it.
- **teams** (`:8326`) direct-send: intentionally omitted (least-privilege; ultra0 is the bridge).
- **greptile** (ECA-100): DROPPED, not just deferred. The operator API key is permanently
  dead — `initialize`/`tools-list` return HTTP 200 but every real data call 401s "Invalid
  API key", and no fresher key exists anywhere on mbpm2. Not a capability loss: pr-review's
  2026-07-11 acceptance run already proved it covers greptile's role via `gh api` instead.
