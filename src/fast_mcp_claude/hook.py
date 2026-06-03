"""PreToolUse hook entry: relay permission requests to a controller peer.

Wiring:
  1. Claude Code (the WORKER session) fires PreToolUse and writes the event JSON
     to this script's stdin.
  2. We call request_approval on the LOCAL fast-mcp-claude server, then long-poll
     await_decision until the CONTROLLER (running on another machine, calling
     into this server via .mcp.json) returns a decision.
  3. We emit a PreToolUse hook response on stdout setting permissionDecision to
     allow/deny/ask.

Env vars consumed:
    CRM_LOCAL_URL        local server URL (default http://127.0.0.1:5473/mcp)
    MCP_API_KEY          bearer token for the local server (REQUIRED if set on server)
    CRM_DECISION_TIMEOUT total seconds to wait for a controller decision (default 300)
    CRM_AUTO_PASS_TOOLS  comma-separated tool names to skip relay for (e.g. "Read,Glob")
    CRM_HOOK_DEBUG       if "1", write debug info to stderr

Failure mode: any error (server down, timeout) falls through to permissionDecision="ask",
so Claude Code's normal permission UI takes over — never silently deny or allow.
"""

import asyncio
import json
import os
import sys
import traceback
from typing import Any


def _debug(msg: str) -> None:
    if os.environ.get("CRM_HOOK_DEBUG") == "1":
        print(f"[crm-hook] {msg}", file=sys.stderr)


def _emit(decision: str, reason: str) -> None:
    """Write a PreToolUse hook response and exit 0."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _fallback_ask(reason: str) -> None:
    _debug(f"falling back to ask: {reason}")
    _emit("ask", f"crm relay unavailable: {reason}")


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception as e:
        _fallback_ask(f"bad stdin: {e}")
        return

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}
    session_id = event.get("session_id") or "default"
    # Truncate session_id to validator's max
    session_id = str(session_id)[:128]

    auto_pass = {
        t.strip() for t in os.environ.get("CRM_AUTO_PASS_TOOLS", "").split(",") if t.strip()
    }
    if tool_name in auto_pass:
        _debug(f"auto-pass: {tool_name}")
        _emit("ask", f"crm relay skipped for {tool_name} (auto-pass)")
        return

    url = os.environ.get("CRM_LOCAL_URL", "http://127.0.0.1:5473/mcp")
    api_key = os.environ.get("MCP_API_KEY")
    try:
        total_timeout = float(os.environ.get("CRM_DECISION_TIMEOUT", "300"))
    except ValueError:
        total_timeout = 300.0

    try:
        decision, reason = asyncio.run(
            _relay(url, api_key, session_id, tool_name, tool_input, total_timeout)
        )
    except Exception as e:
        _debug(f"relay error: {e}\n{traceback.format_exc()}")
        _fallback_ask(str(e))
        return

    _emit(decision, reason)


async def _relay(
    url: str,
    api_key: str | None,
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    total_timeout: float,
) -> tuple[str, str]:
    """Talk to the local fast-mcp-claude server; return (decision, reason)."""
    # Import inside the function so that a missing fastmcp install only breaks
    # the hook path, not the rest of the package.
    from fastmcp import Client

    client_kwargs: dict[str, Any] = {}
    if api_key:
        client_kwargs["headers"] = {"Authorization": f"Bearer {api_key}"}

    async with Client(url, **client_kwargs) as c:
        req = await c.call_tool(
            "request_approval",
            {
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
        )
        data = _result_data(req)
        if not data.get("success"):
            err = (data.get("error") or {}).get("message", "request_approval failed")
            return ("ask", f"crm: {err}")

        approval_id = data["approval_id"]
        _debug(f"approval_id={approval_id} tool={tool_name}")

        elapsed = 0.0
        while elapsed < total_timeout:
            chunk = min(25.0, total_timeout - elapsed)
            res = await c.call_tool(
                "await_decision",
                {"approval_id": approval_id, "timeout": chunk},
            )
            rdata = _result_data(res)
            if not rdata.get("success"):
                err = (rdata.get("error") or {}).get("message", "await_decision failed")
                return ("ask", f"crm: {err}")
            if rdata.get("ready"):
                approval = rdata["approval"]
                decision = approval.get("decision") or "ask"
                reason = (approval.get("reason") or "").strip()
                if decision not in ("allow", "deny"):
                    decision = "ask"
                return (decision, reason or f"controller decided: {decision}")
            elapsed += chunk

    return ("ask", f"controller did not decide within {total_timeout:.0f}s")


def _result_data(result: Any) -> dict[str, Any]:
    """Extract the structured tool result regardless of fastmcp.Client version differences."""
    # fastmcp 3.x: CallToolResult with .data (structured) and .content (list of blocks).
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    # Fallback: parse the first JSON content block.
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
    return {}


if __name__ == "__main__":
    main()
