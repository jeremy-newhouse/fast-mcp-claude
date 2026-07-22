"""fast-mcp-claude-statusline-hook — context/cost telemetry writer for a live dev session.

Registered by the launch wrapper as a Claude Code `statusLine` command (docs.claude.com/en/
statusline). Claude Code invokes it on every new assistant message with a JSON payload on
stdin describing the LIVE context window (`context_window.used_percentage`, non-cumulative as
of CC 2.1.132+ — unlike ResultMessage.usage's cumulative-and-overestimating trap, see
docs/research/sdk-session-management-inventory.md G13) and cost. This hook merges those fields
into the local status file (CRM_SESSION_STATUS_FILE) that fast-mcp-claude-session(-channel)
reads each heartbeat and announces to the mesh — the fleet-wide context-utilization signal
ECA-49 asks for — and prints a plain status line so the operator's terminal keeps a sensible
display.

Deliberately minimal and least-privilege, same posture as session_hook.py: pure stdlib, NO
network, NO mesh bearer. Any error falls back to a minimal static line instead of raising or
printing nothing, since the printed text IS what renders in the operator's prompt.
"""

import json
import os
import sys
import time

from fast_mcp_claude.session_hook import atomic_write_status_file, load_status_file

_FALLBACK_LINE = "claude"


def _status_line(model_name: str | None, pct: float | None, cost_usd: float | None) -> str:
    parts = [model_name or _FALLBACK_LINE]
    if pct is not None:
        parts.append(f"{pct}% context")
    if cost_usd is not None:
        parts.append(f"${cost_usd:.2f}")
    return " · ".join(parts)


def main(argv: list[str] | None = None) -> None:
    line = _FALLBACK_LINE
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = {}
    except (json.JSONDecodeError, ValueError):
        payload = {}

    try:
        model = payload.get("model")
        model_name = model.get("display_name") if isinstance(model, dict) else None
        cw = payload.get("context_window")
        cw = cw if isinstance(cw, dict) else {}
        pct = cw.get("used_percentage")
        tokens_used = cw.get("total_input_tokens")
        window_size = cw.get("context_window_size")
        cost = payload.get("cost")
        cost_usd = cost.get("total_cost_usd") if isinstance(cost, dict) else None

        line = _status_line(model_name, pct, cost_usd)

        path = os.environ.get("CRM_SESSION_STATUS_FILE")
        if path:
            path = os.path.expanduser(path)
            st = load_status_file(path)
            if pct is not None:
                st["context_pct"] = pct
            if tokens_used is not None:
                st["context_tokens_used"] = tokens_used
            if window_size is not None:
                st["context_window_size"] = window_size
            if cost_usd is not None:
                st["cost_usd"] = cost_usd
            st["context_updated_at"] = time.time()
            atomic_write_status_file(path, st)
    except Exception:
        # Never blank the operator's status line on an up-reporting hiccup.
        pass
    finally:
        print(line)


if __name__ == "__main__":
    main()
