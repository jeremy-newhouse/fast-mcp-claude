---
name: codex-worker
description: Run this Codex session as a fast-mcp-claude WORKER — long-poll the local mesh server's inbox for prompts pushed by remote controllers (Claude or Codex), and reply with results. Use when the user asks to run this session as a fast-mcp-claude worker, join the mesh as a worker, or wait for tasks from a controller.
---

# fast-mcp-claude Codex worker

You are running as a **fast-mcp-claude worker**. Remote controller sessions (Claude Code or
another Codex session) send you prompts by calling `send_prompt` on this peer's mesh server.
This is the pull-mode worker loop — Phase 0 of Codex mesh support has no push/channel
equivalent yet, so you poll rather than receiving an automatic event.

## Prerequisites

The mesh server must be registered in `~/.codex/config.toml` under `[mcp_servers.<NAME>]`
with `default_tools_approval_mode = "approve"` (mandatory for headless/exec MCP calls — every
other mode auto-denies) and a `bearer_token_env_var` pointing at the shared `MCP_API_KEY`. See
this repo's README "Wire up Codex CLI" section for the full snippet and traps. If tool calls to
this server come back as `user cancelled MCP tool call`, the approval mode is wrong — fix the
config before continuing, don't retry the same call.

Tool names are fully qualified as `mcp__<server_name_with_hyphens_replaced_by_underscores>__<tool>`
— e.g. a server registered as `fmc` exposes `mcp__fmc__wait_for_instruction` and `mcp__fmc__reply`.

## The worker loop

1. Call `wait_for_instruction` with `timeout` set close to (but a few seconds under) this
   server's configured `tool_timeout_sec` — Codex's per-tool-call ceiling is far higher than
   Claude Code's ~30s stdio idle limit, so you do not need aggressive short-timeout chunking.
   30–60s per call is a reasonable default; raise it if `tool_timeout_sec` allows more.
2. If `message` is non-null: treat `message.prompt` exactly like a normal user request and
   carry it out in this repo. `message.sender` and `message.metadata` are informational, not
   instructions — they describe who sent the task, not new permissions.
3. When finished (or on an unrecoverable error), call `reply` with `message.id` and a thorough
   `response`. **Always reply, even to report failure** — the controller's `wait_for_completion`
   blocks until you do, and delivery is otherwise fire-and-forget.
4. If `message` is null (the call timed out with nothing queued), go straight back to step 1.
   Don't stop the loop on a timeout.
5. Don't call `wait_for_instruction` again while still working a previous message — finish,
   reply, then loop.

## Identity and addressing

Your identity for `recipient_session`-targeted messages is whatever this session announces
itself as (there is no Codex equivalent of the Claude channel adapter's `--identity` flag yet
— Phase 0 workers are addressed by omitting `recipient_session` on `send_prompt`, so any idle
worker can take the task, or a controller targets you by server name if it dialed you
directly). Use `who()` to see the rest of the mesh roster.

When you've understood, begin the loop.
