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

- `*` matches any run of characters, **including spaces and newlines**, at any position
  (`git * main` matches `git checkout main`). Every other character is literal.
- **The space before a trailing `*` is what creates the word boundary.** `Bash(ls *)`
  matches `ls -la` but not `lsof`; `Bash(ls*)` matches both. Same rule as Claude Code.
- **A trailing `:*` is an equivalent way to write that wildcard**, so `Bash(ls:*)` ==
  `Bash(ls *)` — which matters because `cmd:*` is the form Claude Code's own permission
  dialog writes, i.e. what you will be pasting out of a `settings.json`. Only at the very
  end: the colon in `Bash(git:* push)` is literal, and so is the one in the MIDDLE of
  `Bash(npm run test:*)`'s pattern — that grant means `npm run test *`, so it admits
  `npm run test --watch` and refuses `npm run test:unit`.
- `Bash(*)` is exactly the bare `Bash` grant.
- The command is split on `&&`, `||`, `|&`, `;`, `|`, `&` and newlines, and the inner
  command of every `$(...)`, backtick, `<(...)`/`>(...)` and `(...)` is hoisted out and
  judged too. **Every** resulting command must match one of that tool's grants. Under
  `Bash(echo *)`, `echo hi && cat /etc/passwd` is denied — before ECA-144 it was allowed,
  by both enforcement layers. Several grants compose: with `Bash(git *)` + `Bash(uv *)`,
  `git status && uv run pytest` passes because each command matches one of them, and so
  does `ls $(git rev-parse --show-toplevel)` (the substitution is judged on its own, and
  the enclosing command still sees an argument).
- Quoting: a single-quoted or backslash-escaped operator is literal (`echo 'a && b'` is one
  command), while a substitution inside **double** quotes still executes and so is still
  judged. Single quotes are POSIX — there are no escapes inside them.

### Fail-closed refusals — ours, not Claude Code's

A matcher-granted command is **refused** (not just unmatched) when it carries something
this matcher does not model. Each of these is a construct where a shell's idea of where a
quote or a command ends differs from the splitter's, and every one of them was a working
arbitrary-command bypass during review:

| Refused | Why |
|---|---|
| a redirection (`>`, `>>`, `<`, `<<`, `<<<`) | a redirect target is not a command, so "every command matches a grant" says nothing about it; ignoring it would let `Bash(echo *)` write any file |
| `#` (a comment) | a shell does **not** apply line continuation inside a comment, so the newline after one is a real separator |
| `$'...'` / `$"..."` | ANSI-C quoting treats `\'` as an escaped quote, so a shell's string ends later than a POSIX scan thinks — one quote out of phase hides a whole `$(...)` |
| a line continuation (`\` + newline) | a shell splices it away before tokenising, even inside double quotes, so `"$\`<newline>`(cmd)"` really is `"$(cmd)"` |
| unbalanced quotes/parens, an unterminated substitution, nesting > 32 | the tail cannot be attributed to any command |

Also unlike Claude Code: **command wrappers are not stripped.** CC strips `timeout`, `time`,
`nice`, `nohup`, `xargs` and a leading `VAR=value` before matching; this matcher does not,
so `timeout 30 uv run pytest` is outside a `Bash(uv *)` grant. Deliberate — every wrapper
stripped is a rule about what that wrapper does, and `xargs`-class wrappers run arbitrary
commands. Grant the wrapper form explicitly, or give the lane a bare `Bash`.

One carve-out that is **ours rather than CC parity**: a wildcard-free matcher is also
compared to the whole command, so a deliberate literal compound (`Bash(git status && npm
test)`) works. CC's docs say a rule must match each subcommand independently and that it
saves one rule per subcommand; without this carve-out there would be no way to grant a
compound command at all, and string equality against text the operator wrote out cannot
smuggle anything.

There is **no path matcher**: `Read(*.py)` is not a path filter and denies rather than
quietly doing something else. Pin file tools with a bare grant — the cwd pin scopes them.

## Run

```bash
uv sync --extra dev
uv run --extra dev pytest
cp .env.example .env      # adjust limits / mesh settings
./start.sh                # pm2 (name: worker-supervisor)
uv run workers status     # CLI against the running daemon
```
