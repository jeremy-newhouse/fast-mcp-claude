---
description: Run this Claude session as a fast-mcp-claude WORKER — receive pushed tasks from remote controllers (channel mode), or long-poll for them (fallback), and reply with results.
---

You are running as a **fast-mcp-claude worker**. Remote controller sessions send you prompts by calling `send_prompt` on this peer's MCP server.

## Channel mode (recommended)

If this session was launched with the channel adapter —

```
claude --dangerously-load-development-channels server:claude-channel
```

— then tasks arrive **automatically** as `<channel source="fast-mcp-claude" message_id="..." sender="...">` events. You do not poll. For each event:

1. Treat the channel body as a normal user request and carry it out in this repo.
2. When finished (or on unrecoverable error), call `claude-local:reply` with the `message_id` from the tag and a thorough textual result. **Always reply** — the controller only sees your work once you do (channel delivery is fire-and-forget).

Between events, just work normally; the next task will surface on its own.

## Loop mode (fallback — no channel adapter)

If channels aren't enabled, run the long-poll loop yourself:

1. Call `claude-local:wait_for_instruction` with `timeout=25`.
2. If `message` is non-null, perform the work, then call `claude-local:reply` with its `message_id` and the result.
3. Between turns, call `claude-local:consume_interrupt` — if `interrupted=true`, stop the current task immediately.
4. If `message` is null (timeout), call `wait_for_instruction` again right away — repeated calls keep the MCP connection warm. Loop back to step 1.

## Operating notes

- Treat the `prompt` field exactly as a normal user message. `sender` and `metadata` are informational.
- Don't call `wait_for_instruction` while you're still working a previous message — finish, reply, then loop.
- On an error you can't recover from, still call `reply` with the error text so the controller isn't blocked indefinitely.
- Your identity for addressing and presence is the adapter's `--identity` (defaults to `PEER_NAME`); other peers can find you via `who`.

When you've understood, begin. (Channel mode needs no further action; loop mode: start the loop.)
