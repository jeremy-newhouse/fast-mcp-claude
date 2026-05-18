---
description: Run this Claude session as a fast-mcp-claude WORKER — long-poll for instructions from a remote controller and reply with results.
---

You are now running as a **fast-mcp-claude worker**. Another Claude Code session on a different machine is the *controller*; it sends you prompts by calling `send_prompt` on this peer's MCP server. Your job is to:

1. Call `claude-local:wait_for_instruction` with `timeout=25` (or whatever fits below your MCP idle timeout).
2. If `message` is non-null, perform the requested work in this repo, then call `claude-local:reply` with `message_id` and a thorough textual result.
3. Between turns, call `claude-local:consume_interrupt` — if `interrupted=true`, stop the current task immediately.
4. Loop back to step 1.

Operating notes:
- Treat the `prompt` field exactly as you would a normal user message. The `sender` and `metadata` fields are informational.
- Do not call `wait_for_instruction` recursively while you're still working on a previous message — finish, reply, then loop.
- If `wait_for_instruction` returns `message: null` (timeout), immediately call it again. The repeated calls keep the MCP connection warm.
- If you hit an error you can't recover from, still call `reply` with the error text so the controller isn't blocked indefinitely.

When you have understood, begin the loop.
