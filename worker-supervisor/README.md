# worker-supervisor

The SDK worker supervisor: autonomous worker fleets as **Claude Agent SDK session chains**
owned by a local daemon, replacing interactive TUI + channel-sidecar workers (the ultra
pattern) for unattended work. One daemon per fleet host; the attended orchestrator drives
it over a local CLI.

**Design of record** (in the `evolv-coder-agent` repo — do not redesign here):

- `docs/adr/0028-sdk-worker-supervisor.md` — decision + operator amendments A1-A9
- `docs/architecture/worker-supervisor.md` — components, lifecycle, config keys
- `docs/design/worker-supervisor-requirements.md` — FR-WS1-11; AC-WS-1-11 are the build gates
- `docs/research/sdk-session-management-inventory.md` — reuse map + SDK gotchas G1-G11

Tracked as **ECA-60** (Backlog in evolv-coder-agent).

## Shape

- **Worker** = named, long-lived, continuity-bearing agent. An **epoch** is one SDK session
  chain (one context window); **cycling** ends an epoch (handover write) and opens the next
  (handover restore). Turns are per-turn `query()` + `resume` — no kept-alive clients.
- **Registry** (SQLite) is the dedup + recovery authority: workers / epochs / turns /
  questions. Turn records are minted before the subprocess spawns; boot reconciliation
  redelivers claimed-but-non-terminal turns.
- **Permission gate**: every worker tool call routes through `can_use_tool` — cwd pin,
  per-worker tool ceiling, optional repo guard hooks, default deny. `AskUserQuestion`
  parks as an escalation answered via the CLI.
- **Control surface**: `workers` CLI over a unix socket (local-only, JSON out):
  `spawn / prompt / status / questions / answer / cycle / kill / events / attach / history`.
- **Auth**: the host's logged-in Claude CLI subscription. Worker env is allowlist-built;
  API/Bedrock credential vars structurally cannot reach a worker.

## Run

```bash
uv sync --extra dev
uv run --extra dev pytest
cp .env.example .env      # adjust limits / mesh settings
./start.sh                # pm2 (name: worker-supervisor)
uv run workers status     # CLI against the running daemon
```
