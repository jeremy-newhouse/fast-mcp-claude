---
id: doc-3
title: Codex CLI mesh support — feasibility research (2026-08-02)
type: specification
created_date: '2026-08-03 00:15'
updated_date: '2026-08-03 00:15'
---
# Codex CLI in the fast-mcp-claude mesh — feasibility research

**Question:** can we support OpenAI Codex CLI with functionality comparable to the Claude Code integration, so that Codex sessions and Claude sessions can talk to each other?

**Answer: yes.** Nothing about the mesh server is Claude-specific — it is a plain streamable-HTTP MCP server with bearer auth, and Codex is a competent MCP client. Three of the four legs (controller, headless worker, permission relay) map onto documented Codex surfaces; the fourth (push into a *live* interactive session) has a verified turn-boundary mechanism and an experimental full-parity mechanism.

In several respects Codex is a **better** host than Claude Code: MCP tool calls get a 300 s timeout (vs. Claude Code's ~30 s stdio idle ceiling that forces our `POLL_MAX_WAIT_S ≤ 25` chunking), hooks include a dedicated `PermissionRequest` event that fires exactly when a dialog would open, and `codex app-server` is a stable-shaped JSON-RPC session-control protocol rather than a research-preview channel API.

**Environment:** `codex-cli 0.145.0`, macOS (mini2), against the live local mesh server on `127.0.0.1:5473`. Codex moves fast — re-run the appendix probes before trusting any of this on a newer build.

---

## 1. Verified empirically (not inferred)

| # | Claim | Result |
|---|---|---|
| 1 | A Codex session can call our mesh tools over HTTP + bearer | **PASS** — `who()` returned the full live roster (5 peers, incl. the Claude sessions and the launcher) |
| 2 | Long-poll tools survive well past Claude Code's ~30 s ceiling | **PASS** — `subscribe(timeout=45)` completed normally; `DEFAULT_TOOL_TIMEOUT = 300s` (`codex-rs/codex-mcp/src/rmcp_client.rs:93`), per-server `tool_timeout_sec` override |
| 3 | A `Stop` hook can push a prompt into a running session | **PASS** — `{"decision":"block","reason":"…"}` produced `hook: Stop Blocked`, the model continued and emitted the injected instruction, then the second `Stop` passed through |
| 4 | MCP calls need an approval knob in headless mode | **PASS (gotcha)** — only `default_tools_approval_mode = "approve"` works; `auto`, `writes` and `prompt` all fail with `user cancelled MCP tool call` |
| 5 | `codex mcp-server` exposes conversation control as MCP tools | **PASS** — two tools: `codex` (start a session: `prompt`, `cwd`, `model`, `sandbox`, `approval-policy`, `config`, …) and `codex-reply` (`threadId` + `prompt`) |
| 6 | Project-level `<repo>/.codex/hooks.json` is discovered | **FAIL on 0.145.0** — never invoked; only `$CODEX_HOME/hooks.json`, `config.toml [hooks]`, and `-c hooks.Stop=[…]` worked. (The published docs describe project-level discovery — they describe `main`, which is ahead of the shipped build.) |

Finding 4 is the sharpest install-time trap: an unannotated MCP tool is treated as non-read-only, so a headless Codex worker silently *auto-denies* every mesh call and reports `isError: true` without ever reaching the server. Two fixes, both worth doing — set `default_tools_approval_mode = "approve"` on the mesh server entry, and add MCP `readOnlyHint` annotations to our read-only tools (`who`, `list_messages`, `pending_approvals`, `read_file`, `list_files`, `subscribe`) so the safer `auto`/`writes` modes also work.

---

## 2. Component mapping

| fast-mcp-claude piece | Codex mechanism | Verdict |
|---|---|---|
| `server.py`, `tools/*` (the mesh itself) | nothing — Codex is just another MCP client | **No change.** Add `readOnlyHint` annotations (see above) |
| `.mcp.json` | `~/.codex/config.toml` `[mcp_servers.NAME]` with `url` + `bearer_token_env_var`, or `codex mcp add NAME --url … --bearer-token-env-var …` | **Config only** |
| `hook.py` (PreToolUse permission relay) | Codex `PreToolUse` hook — same stdin fields (`session_id`, `tool_name`, `tool_input`, `cwd`, `transcript_path`, `permission_mode`, `model`, plus `turn_id`/`tool_use_id`) and the same stdout shape (`hookSpecificOutput.permissionDecision`) | **~90 % reusable**, two deltas below |
| — better target | `PermissionRequest` hook: fires *only* when Codex would actually prompt — precisely `channel.py`'s relay semantics. Output `hookSpecificOutput.decision.behavior = allow\|deny`; any `deny` wins across matching hooks | **Cleaner than the Claude path** |
| `session_hook.py` (status file) | `SessionStart` / `UserPromptSubmit` / `Stop` all exist with matching payload fields (+ `source` on SessionStart) | **Straight port** |
| `statusline_hook.py` (context %, cost) | **no analog** — Codex has no `statusLine` config | **Gap** — see §5 |
| `session.py` (presence announcer / notify+pull) | pure mesh client + status-file reader; no Claude internals | **Straight port** |
| `channel.py` (push + permission relay) | no direct analog; three architectures in §4 | **Design work** |
| `launcher.py` (headless exec leg) | `codex exec`: prompt via argv/stdin, `--json` JSONL events, `-o/--output-last-message`, `--output-schema`, `-C/--cd`, `--add-dir`, `--sandbox {read-only,workspace-write,danger-full-access}`, `exec resume --last\|<id>`, `--ephemeral`, `--ignore-user-config` | **Straight port** (swap the subprocess argv) |
| tools ceiling (`--tools`) | sandbox mode + `approval_policy` + `[mcp_servers.*.enabled_tools/disabled_tools]` + execpolicy `.rules` | **Equivalent, different shape** |
| `/worker`, `/control`, `/fleet-inbox` skills | Codex skills (`$CODEX_HOME/skills`, `skill_search`), slash commands, `AGENTS.md`; `codex plugin` + marketplaces for fleet distribution | **Straight port** |

### Permission-relay deltas

1. **No `"ask"`.** `permissionDecision: "ask"` is parsed but rejected. Our fail-safe (`_fallback_ask`) must become **exit 0 with empty stdout** — no decision, so Codex's own approval policy / TUI dialog takes over. Same end behavior, different wire encoding, and it must be got right: emitting `"ask"` is an error, and emitting `"deny"` on timeout would break the "never silently deny" invariant.
2. **No 25 s chunk loop needed.** Hook timeout is per-hook configurable (docs: 600 s default, `SessionEnd` 1 s), so a single `await_decision` call covers our 300 s `CRM_DECISION_TIMEOUT`. Keeping the loop is harmless.
3. `updatedInput` lets a hook *rewrite* tool arguments — a capability the Claude path lacks. On `PermissionRequest`, `updatedInput` / `updatedPermissions` / `interrupt` are reserved and **fail closed** if present.
4. `PreToolUse` matchers can intercept `spawn_agent`, i.e. subagent creation is governable.

---

## 3. What Codex adds that Claude Code lacks

- **`codex mcp-server`** — a controller can drive a Codex *conversation* directly as two MCP tools (`codex`, `codex-reply`), no launcher process required. A Claude controller could spawn Codex work by wiring `codex mcp-server` into its own `.mcp.json`.
- **`codex app-server`** — 89 client methods incl. `thread/start`, `thread/resume`, `turn/start`, `turn/steer` (append input *mid-turn*), `turn/interrupt`, `thread/inject_items` (silent history injection), `thread/list`, `thread/loaded/list`. Approvals arrive as JSON-RPC server→client **requests** (`execCommandApproval`, `applyPatchApproval`, `item/permissions/requestApproval`, `item/tool/requestUserInput`, `mcpServer/elicitation/request`) that the client answers — a native permission relay with no hook in the path. Transports: stdio, `unix://PATH`, `ws://IP:PORT`.
- **`codex remote-control start|stop|pair`** + TUI `--remote ws://…` — OpenAI's own remote-attach path, with short-lived pairing codes.
- **`approvals_reviewer = auto_review`** — a built-in subagent reviewer for approvals, conceptually overlapping our Phase-3 approval relay.

---

## 4. Three architectures for a live Codex session

**Option A — `Stop`-hook drain (verified, recommended first).** At each turn end the hook calls `wait_for_instruction`; if a message is queued it returns `{"decision":"block","reason":"<prompt + message_id>"}`, so the session works the task in-band and the agent replies through the mesh `reply` tool — the same contract `channel.py` uses. `stop_hook_active` plus one-in-flight claiming keeps it loop-safe.
*Cost:* delivery happens only at turn boundaries — an idle session receives nothing until the operator does something. A long-polling Stop hook would make the session look busy, so default to a 0–2 s poll and gate any longer window behind an "armed worker" flag file.

**Option B — app-server sidecar (full parity, experimental).** Sidecar owns a thread over `unix://`/`ws://`: `turn/start` to push, `turn/steer` to interject mid-turn, `turn/interrupt` to cancel, approvals answered inline. True idle-session push and a permission relay with no hook. *Unverified:* whether two clients can attach to one thread and whether an operator-attached TUI (`codex --remote`) sees sidecar-injected turns. The CLI marks it experimental, and the `remote_control` feature flag reads `removed` while the subcommand still exists — churn risk.

**Option C — headless only (`codex exec` via a launcher).** No live push at all; Codex is a worker. Lowest risk, fastest to ship, and it already covers "sessions can talk to each other" in the controller→worker direction.

---

## 5. Gaps and open questions

1. **No `statusLine` analog** → no `context_pct` / `context_tokens_used` / `cost_usd` in presence metadata. Workarounds: parse the rollout JSONL at `transcript_path`; take `thread/tokenUsage/updated` from app-server; or `otel` export. Otherwise Codex peers show degraded roster entries.
2. **Idle-session push needs Option B.** Option A cannot wake a session that is sitting at the prompt.
3. **Hook trust.** Non-managed hooks are hash-pinned and need review via `/hooks`; changing the script re-arms the prompt. Automation needs `--dangerously-bypass-hook-trust`, or managed hooks declared in `requirements.toml` (which also supports `allow_managed_hooks_only`). This shapes how `worker-supervisor` / `spawner` install a Codex lane.
4. **Install hooks into `$CODEX_HOME` or `config.toml`, not `<repo>/.codex/hooks.json`** (finding 6).
5. **`default_tools_approval_mode` is mandatory** for headless Codex workers (finding 4).
6. Does a long-running `Stop` hook block operator input in the TUI? Assumed yes — verify before choosing any poll window > ~2 s.
7. `notify` is legacy (`codex-rs/hooks/src/legacy_notify.rs`); prefer hooks.
8. Feature-flag churn is real (`remote_control` = `removed`, `multi_agent` = stable, `multi_agent_v2` = off). Pin behavior with probes, not with docs.

---

## 6. Security notes

- **Same bearer, more clients.** A Codex session holding `MCP_API_KEY` can call *every* mesh tool including `approve_tool` — i.e. self-approval. Replicate the existing mitigation: the launcher holds the bearer and the hook asks it over a unix socket (`CRM_HOOK_SOCKET`), so the worker never sees a mesh credential. Do **not** put `MCP_API_KEY` in a Codex worker's env if that worker's permission relay is meant to be trustworthy.
- `bearer_token_env_var` keeps the key in the Codex process env, which the model can read via a shell command unless `shell_environment_policy` filters it. Set that policy for worker lanes.
- `MCP_ADMIN_API_KEY` / `triggering_admin` semantics are unchanged and server-verified (FMC-9), so a Codex peer cannot forge admin origin.
- `--dangerously-bypass-hook-trust` and `--dangerously-bypass-approvals-and-sandbox` are both accurately named. Prefer managed hooks + a sandbox mode for fleet installs.

---

## 7. Recommended phasing

| Phase | Scope | Effort |
|---|---|---|
| **0** | Config only: register the mesh server in `~/.codex/config.toml` (`default_tools_approval_mode="approve"`), ship `codex-worker` / `codex-control` skills, add `readOnlyHint` annotations to read-only mesh tools. A Codex session can then control Claude workers and be driven via the pull idiom (`wait_for_instruction` → `reply`). | hours |
| **1** | `codex exec` worker leg — port `launcher.py` (or give it a pluggable engine). Headless Codex workers join the mesh. | small |
| **2** | Codex hooks: `hook.py` → `PreToolUse` + `PermissionRequest`; `session_hook.py` → status file. Delivers the permission relay and presence. | small–medium |
| **3** | `Stop`-hook drain (Option A) for live Codex sessions. | medium |
| **4** | app-server sidecar (Option B) for true channel parity. | research |

Phase 0 alone already satisfies "sessions can talk to each other" in both directions with **zero code changes** — only configuration and prompt/skill work.

---

## Appendix — reproduction

```bash
# 1 + 2: Codex as a mesh client, incl. a 45 s long-poll (needs MCP_API_KEY exported)
codex exec --sandbox read-only --skip-git-repo-check \
  -c 'mcp_servers.fmc.url="http://127.0.0.1:5473/mcp"' \
  -c 'mcp_servers.fmc.bearer_token_env_var="MCP_API_KEY"' \
  -c 'mcp_servers.fmc.default_tools_approval_mode="approve"' \
  -c 'mcp_servers.fmc.tool_timeout_sec=120' \
  'Call ONLY the fmc MCP tool subscribe with channel="codex-probe", after_id=0, timeout=45. Report its raw JSON.'

# 3: Stop-hook push injection (inject.sh emits {"decision":"block","reason":"…"} once)
codex exec --sandbox read-only --dangerously-bypass-hook-trust \
  -c "hooks.Stop=[{hooks=[{type=\"command\",command=\"bash $PWD/inject.sh\",timeout=30}]}]" \
  'Reply with exactly: READY'

# 5: enumerate what codex mcp-server exposes (initialize + tools/list over stdio)
codex mcp-server

# surface inventory
codex features list
codex app-server generate-json-schema --out ./asschema   # 89 client methods, 70 notifications
```

Authoritative sources read: `codex-rs/hooks/src/schema.rs` (hook wire contracts), `codex-rs/config/src/hook_config.rs` (11 events), `codex-rs/config/src/mcp_types.rs` + `codex-rs/core/config.schema.json` (MCP server config), `codex-rs/codex-mcp/src/rmcp_client.rs` (timeouts), `codex-rs/app-server/README.md` (protocol), locally generated app-server JSON Schema.
