# sandbox-runner (ECA-64)

The **in-container SDK runner** and **hardened image** for the Docker agent
sandbox. Design of record: `evolv-coder-agent/docs/architecture/container-sandbox.md`,
ADR-0024. A self-contained uv subproject, sibling to `worker-supervisor/`, but
deliberately **leaner** — the image pins `claude-agent-sdk==0.2.91` and nothing
else (no MCP, no NATS, no coordination deps). The container **is** the security
boundary; the per-peer **spawner (ECA-65)** is the only launcher and owns the
run flags, secret mounts, egress network, and result publication.

## What one invocation does

`python -m sandbox_runner --job-dir /job` reads `/job/request.json`, optionally
clones a repo via a credential-helper (token never in argv/URL/layers), drives
**one** hermetic `claude_agent_sdk` session under the limits triple, and relays:

- `/job/events.jsonl` — appended + fsync'd live (spawner tails → `.event`)
- `/job/result.json` — written **last** via atomic rename (spawner → `.result`)

### `request.json`

```json
{
  "job_id": "abc123",
  "prompt": "Summarize the README in three bullets.",
  "repo": { "url": "https://github.com/owner/repo.git", "ref": "main", "clone": true },
  "model": "us.anthropic.claude-...",
  "limits": { "wall_clock_s": 1800, "max_turns": 50, "max_budget_usd": 10.0 }
}
```

### `result.json` states

`completed` · `timeout` · `budget_exceeded` · `turn_limit` · `error`. Every
result carries `total_cost_usd` + `usage` (per-job cost/DoS raw data). The
process exits `0` whenever a result was written — job-level failures are read
from `state`, not the exit code.

## Limits enforcement

| Limit | Mechanism |
|---|---|
| `max_turns` | native `ClaudeAgentOptions.max_turns` |
| `max_budget_usd` | **native** `ClaudeAgentOptions.max_budget_usd` (present in 0.2.91) |
| `wall_clock_s` | runner `asyncio.timeout` → cancel transport → SIGTERM/SIGKILL |

## Secrets (AC#3)

- **GitHub token** — `0400` bind-mount at `/run/secrets/gh_token`; read lazily by
  git's credential helper at clone time. Never in argv/URL/`.git/config`/layers.
- **Bedrock bearer** — `0400` bind-mount at `/run/secrets/bedrock`; `entrypoint.sh`
  exports it into the runner process env **only** (never container-wide `-e`).

## Build & run

```sh
docker build -t eca/agent-sandbox:dev .
# The spawner (ECA-65) supplies the hardening flags; see smoke/ for the full
# hardened invocation (cap-drop, read-only rootfs, seccomp, egress network).
```

## Dev gates

```sh
uv venv && uv pip install -e '.[dev]'
uv run ruff check .
uv run pytest          # pure tests always run; runner tests skip w/o the SDK
```

## Layout

```
Dockerfile .dockerignore entrypoint.sh   # hardened image (AC#1/AC#3)
src/sandbox_runner/
  __main__.py   # CLI: read request.json, clone, run, relay
  runner.py     # hermetic ClaudeAgentOptions + limits + message→event fold
  limits.py     # {wall_clock_s, max_turns, max_budget_usd}
  gitcreds.py   # credential-helper clone (token never in argv/layers)
  result.py     # events.jsonl (live) + result.json (atomic, last)
seccomp/ apparmor/ egress-proxy/          # host-side controls (spawner-applied)
tests/  smoke/                            # unit + end-to-end (AC#5)
```

## Residuals (stated, not hidden)

On macOS Docker Desktop: **AppArmor is silently ignored** (seccomp is the MAC
substitute; the profile ships for the Linux/ECS path), **userns-remap** isn't
configurable (the VM is the boundary), and the **egress allowlist is a
speed-bump, not an exfil boundary** — the real mitigation is the operator's
fine-grained, short-TTL token. See `container-sandbox.md` finding 15.
