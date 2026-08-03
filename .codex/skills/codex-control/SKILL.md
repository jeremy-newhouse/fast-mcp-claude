---
name: codex-control
description: Adopt the fast-mcp-claude CONTROLLER role from a Codex session — drive a remote Claude or Codex worker via send_prompt / wait_for_completion / approve_tool. Use when the user asks to control a remote peer, drive another mesh session, or delegate work to a fast-mcp-claude worker.
---

# fast-mcp-claude Codex controller

You are now the **fast-mcp-claude controller**. The user's task should be carried out (in
whole or part) on a remote peer — another machine, or another session — reachable through a
mesh server registered in `~/.codex/config.toml` under `[mcp_servers.<NAME>]`. The remote peer
may be a Claude Code worker/launcher or another Codex worker; the mesh protocol is identical
either way.

## Prerequisites

`default_tools_approval_mode = "approve"` must be set on the target server entry, or every
mesh tool call auto-denies headlessly with `user cancelled MCP tool call` before it ever
reaches the server. Tool names are fully qualified as
`mcp__<server_name_with_hyphens_replaced_by_underscores>__<tool>` — e.g. a peer registered as
`fmc-peer-laptop` exposes `mcp__fmc_peer_laptop__send_prompt`.

## The control loop

1. Pick the remote peer's registered server name. Call `who()` on it to see who's online
   (identity + what each is working on) if you need to target a specific session rather than
   any idle worker.
2. Call `send_prompt` with:
   - `prompt`: the user message you want the remote to act on
   - `sender`: your own peer identity (so the remote sees who is talking)
   - `recipient_session` (optional): a specific identity from `who()` — omit to let the next
     idle worker take it
   - `metadata`: any structured context the remote should see
   It returns a `message_id`.
3. Call `wait_for_completion` with that `message_id` and a `timeout`. Codex's per-tool-call
   ceiling (`tool_timeout_sec`, configurable, well above Claude Code's ~30s stdio idle limit)
   means you can use one longer wait instead of chunking into many short polls — but still loop
   if it returns without `ready=true`, since the remote's own work may outlast a single call.
   Between waits:
   - If the remote needs a permission decision, its `pending_approvals` will have entries —
     call `approve_tool(approval_id, decision, reason)` to unblock its PreToolUse hook.
   - Use `get_status()` for a liveness check.
4. Surface the remote's `response` back to the user and decide the next step.

## Things to remember

- Each remote is an independent session with its own working directory and tool surface. You
  are not directly editing its files — you are asking it to.
- The remote's response is plain text. If you need structured data, ask the remote to return
  JSON in its reply.
- A Claude Code peer's headless launcher lane (if online) is a valid worker target too — it
  spawns a `claude -p` subprocess per task and replies the same way an interactive worker would.
  **It requires `prompt` to be a JSON task envelope, not plain text** —
  `{"task": "...", "cwd": "<absolute path inside its cwd_allowlist>"}` (optional
  `allowed_tools`/`model`/`timeout_s`) — a plain-text prompt fails with
  `{"ok": false, "error": "bad_envelope", ...}` before any work happens (verified live for
  FMC-18). An interactive/channel worker, by contrast, takes `prompt` as plain text.

When you've understood, begin.
