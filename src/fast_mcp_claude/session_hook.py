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

import contextlib
import fcntl
import json
import os
import sys
import time

_MAX_LAST = 200

# SessionStart's `source` values for a context CUT (clear/compact discard prior context, so a
# stale context_pct/message_count would misreport "clean recommended" right after one) versus
# continuity-preserving sources (startup: the wrapper just reseeded the file fresh anyway;
# resume/fork: genuinely continue prior context, must NOT reset). docs.claude.com/en/hooks.
_CONTEXT_CUT_SOURCES = {"clear", "compact"}


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


@contextlib.contextmanager
def locked_status_file(path: str):
    """Shared with statusline_hook.py: holds an exclusive flock across a full read-modify-
    write cycle on `path`. Both hooks fire independently (SessionStart/UserPromptSubmit/Stop
    from the CC hook chain, statusLine on every new assistant message) and each does a
    load_status_file -> mutate -> atomic_write_status_file round trip over the SAME file — an
    unlocked pair can interleave and lose one side's update (a stale full-dict write silently
    reverts whatever the other process wrote in between). ECA-49 follow-up: closes that race."""
    if not os.path.exists(path):
        with contextlib.suppress(FileExistsError):
            open(path, "x", encoding="utf-8").close()
    with open(path, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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

        with locked_status_file(path):
            st = load_status_file(path)
            st.setdefault("started_at", time.time())
            # Seed static fields if the wrapper didn't (defensive; the wrapper normally does).
            if event.get("cwd") and not st.get("cwd"):
                st["cwd"] = event["cwd"]
            if not st.get("repo") and st.get("cwd"):
                st["repo"] = os.path.basename(str(st["cwd"]).rstrip("/"))
            # Every CC hook event carries session_id (same field hook.py:69 already reads);
            # publish it so presence can surface which underlying Claude session this live
            # session is (ECA-23).
            if event.get("session_id"):
                st["claude_session_id"] = event["session_id"]

            if name == "UserPromptSubmit":
                st["status"] = "working"
                prompt = (event.get("prompt") or "").strip().replace("\n", " ")
                if prompt:
                    st["last"] = prompt[:_MAX_LAST]
                # ECA-49: one increment per operator turn, so fleet-wide presence can surface
                # session activity volume alongside context/cost (statusline_hook.py's twin).
                st["message_count"] = st.get("message_count", 0) + 1
            elif name == "Stop":
                st["status"] = "idle"
            elif name == "SessionStart":
                st["status"] = "active"
                if event.get("source") in _CONTEXT_CUT_SOURCES:
                    # ECA-49: a clear/compact discards prior context IN THE SAME long-lived
                    # process (no re-exec of start-session.sh, so the file is never re-seeded)
                    # — drop the now-stale telemetry so a post-clear read doesn't still show
                    # the pre-clear context_pct/message_count.
                    for key in (
                        "message_count", "context_pct", "context_tokens_used",
                        "context_window_size", "cost_usd", "context_updated_at",
                    ):
                        st.pop(key, None)
            else:
                st.setdefault("status", "active")
            st["updated_at"] = time.time()

            atomic_write_status_file(path, st)
    except Exception:
        # Never block or fail the session on an up-reporting hiccup.
        pass


if __name__ == "__main__":
    main()
