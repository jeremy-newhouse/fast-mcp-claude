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
- **Permission gate**: every worker tool call routes through a **PreToolUse policy hook** —
  cwd pin, per-worker tool ceiling, optional repo guard hooks, default deny. The hook, not
  `can_use_tool`, is the total point: the CLI auto-approves some calls without ever
  consulting `can_use_tool` (ECA-142). `AskUserQuestion` parks as an escalation answered
  via the CLI. Grant syntax below.
- **Control surface**: `workers` CLI over a unix socket (local-only, JSON out):
  `spawn / prompt / status / questions / answer / cycle / kill / events / attach / history`.
- **Auth**: the host's logged-in Claude CLI subscription. Worker env is allowlist-built;
  API/Bedrock credential vars structurally cannot reach a worker.

## Writing a tool ceiling (`workers spawn --tools`)

Three grant shapes. `WorkerPolicy.ceiling_denial` in `gate.py` is the implementation of
record; this is the contract an operator authors against.

| Shape | Example | Means |
|---|---|---|
| bare name | `Bash` | that tool, **every** input |
| name wildcard | `mcp__jira__*` | every tool whose NAME starts with the prefix — how a whole MCP server is granted |
| command matcher | `Bash(uv run *)` | that tool, only for commands matching the pattern |

**A matcher uses Claude Code's `settings.json` syntax, and is applied to every command in
a compound command** (ECA-144) — so a granted prefix cannot smuggle a second command:

- `*` matches any run of characters, **including spaces**, at any position (`git * main`
  matches `git checkout main`). Every other character is literal — `:` included, so
  `Bash(npm run test:*)` means what it looks like.
- **The space before a trailing `*` is what creates the word boundary.** `Bash(ls *)`
  matches `ls -la` but not `lsof`; `Bash(ls*)` matches both. Same rule as Claude Code.
- The command is split on `&&`, `||`, `|&`, `;`, `|`, `&` and newlines, and the inner
  command of every `$(...)`, backtick, `<(...)`/`>(...)` and `(...)` is hoisted out and
  judged too. **Every** resulting command must match one of that tool's grants. Under
  `Bash(echo *)`, `echo hi && cat /etc/passwd` is denied — before ECA-144 it was allowed,
  by both enforcement layers. Several grants compose: with `Bash(git *)` + `Bash(uv *)`,
  `git status && uv run pytest` passes because each command matches one of them.
- Quoting is honoured as a shell honours it: a single-quoted or backslash-escaped operator
  is literal (`echo 'a && b'` is one command), while a substitution inside **double**
  quotes still executes and so is still judged.

**Two deliberate deltas from Claude Code, both fail-closed.** A matcher-granted command
carrying a **redirection** (`>`, `>>`, `<`, `<<`, `<<<`) is refused — a redirect target is
not a command, so "every command matches a grant" says nothing about it, and ignoring it
would let `Bash(echo *)` write any file. A command that cannot be parsed (unbalanced quote,
unterminated substitution) is refused rather than guessed at. A lane that genuinely needs
either wants a bare `Bash` grant. A wildcard-free matcher is also compared to the whole
command, so a deliberate literal compound (`Bash(git status && npm test)`) still works.

There is **no path matcher**: `Read(*)` is not a path filter and denies rather than
allowing every Read. Pin file tools with a bare grant — the cwd pin scopes them.

## Run

```bash
uv sync --extra dev
uv run --extra dev pytest
cp .env.example .env      # adjust limits / mesh settings
./start.sh                # pm2 (name: worker-supervisor)
uv run workers status     # CLI against the running daemon
```
