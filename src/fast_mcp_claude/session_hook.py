"""fast-mcp-claude-session-hook — up-reporting status writer for a live dev session.

Registered by the launch wrapper as a Claude Code SessionStart / UserPromptSubmit / Stop
hook. On each event it does ONE thing: read the CC hook event JSON on stdin and update the
local status file (CRM_SESSION_STATUS_FILE) that fast-mcp-claude-session reads each heartbeat
and announces to the mesh. So "what's it working on" is answerable from presence.

Deliberately minimal and least-privilege: pure stdlib, NO network, NO mesh bearer. The
sidecar (which holds the bearer) is the sole announcer; the hook only writes a local file —
so even a compromised hook can't talk to the mesh. It never blocks the session: any error
exits 0 silently, and it emits no hook output (no permission decision, no added context).
"""

import json
import os
import sys
import time

_MAX_LAST = 200


def load_status_file(path: str) -> dict:
    """Shared with statusline_hook.py: both merge fields into the same status file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_write_status_file(path: str, data: dict) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> None:
    try:
        path = os.environ.get("CRM_SESSION_STATUS_FILE")
        if not path:
            return
        path = os.path.expanduser(path)
        try:
            event = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            event = {}
        name = event.get("hook_event_name") or event.get("hookEventName") or ""

        st = load_status_file(path)
        st.setdefault("started_at", time.time())
        # Seed static fields if the wrapper didn't (defensive; the wrapper normally does).
        if event.get("cwd") and not st.get("cwd"):
            st["cwd"] = event["cwd"]
        if not st.get("repo") and st.get("cwd"):
            st["repo"] = os.path.basename(str(st["cwd"]).rstrip("/"))
        # Every CC hook event carries session_id (same field hook.py:69 already reads); publish
        # it so presence can surface which underlying Claude session this live session is (ECA-23).
        if event.get("session_id"):
            st["claude_session_id"] = event["session_id"]

        if name == "UserPromptSubmit":
            st["status"] = "working"
            prompt = (event.get("prompt") or "").strip().replace("\n", " ")
            if prompt:
                st["last"] = prompt[:_MAX_LAST]
            # ECA-49: one increment per operator turn, so fleet-wide presence can surface
            # session activity volume alongside context/cost (statusline_hook.py's twin fields).
            st["message_count"] = st.get("message_count", 0) + 1
        elif name == "Stop":
            st["status"] = "idle"
        elif name == "SessionStart":
            st["status"] = "active"
        else:
            st.setdefault("status", "active")
        st["updated_at"] = time.time()

        atomic_write_status_file(path, st)
    except Exception:
        # Never block or fail the session on an up-reporting hiccup.
        pass


if __name__ == "__main__":
    main()
