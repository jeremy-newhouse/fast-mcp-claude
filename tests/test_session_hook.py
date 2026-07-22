"""Tests for fast-mcp-claude-session-hook — the up-reporting status writer.

It reads a CC hook event on stdin and updates the local status file the sidecar reads.
Pure stdlib, no network: a missing status-file env is a clean no-op; it preserves static
fields the wrapper seeded; and it maps SessionStart/UserPromptSubmit/Stop to status/last.
"""

import io
import json

from fast_mcp_claude import session_hook


def _run(monkeypatch, event: dict, status_path: str | None):
    if status_path is None:
        monkeypatch.delenv("CRM_SESSION_STATUS_FILE", raising=False)
    else:
        monkeypatch.setenv("CRM_SESSION_STATUS_FILE", status_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    session_hook.main([])


def test_no_status_file_env_is_noop(monkeypatch):
    # Must not raise even with a real event and no target path.
    _run(monkeypatch, {"hook_event_name": "Stop"}, None)


def test_user_prompt_submit_sets_working_and_last(tmp_path, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"identity": "mini2.r", "machine": "mini2", "repo": "r"}))
    _run(monkeypatch,
         {"hook_event_name": "UserPromptSubmit", "prompt": "fix the auth bug\nnow"}, str(sf))
    data = json.loads(sf.read_text())
    assert data["status"] == "working"
    assert data["last"] == "fix the auth bug now"  # newlines collapsed
    assert data["machine"] == "mini2" and data["repo"] == "r"  # static fields preserved
    assert "updated_at" in data and "started_at" in data


def test_stop_sets_idle(tmp_path, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r", "status": "working", "last": "x"}))
    _run(monkeypatch, {"hook_event_name": "Stop"}, str(sf))
    data = json.loads(sf.read_text())
    assert data["status"] == "idle"
    assert data["last"] == "x"  # last preserved across a Stop


def test_session_start_sets_active(tmp_path, monkeypatch):
    sf = tmp_path / "s.json"
    _run(monkeypatch, {"hook_event_name": "SessionStart", "cwd": "/repos/demo"}, str(sf))
    data = json.loads(sf.read_text())
    assert data["status"] == "active"
    # seeds cwd/repo defensively if the wrapper didn't
    assert data["cwd"] == "/repos/demo" and data["repo"] == "demo"


def test_long_prompt_is_truncated(tmp_path, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r"}))
    _run(monkeypatch, {"hook_event_name": "UserPromptSubmit", "prompt": "z" * 500}, str(sf))
    assert len(json.loads(sf.read_text())["last"]) == 200


def test_session_id_is_captured(tmp_path, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r"}))
    _run(monkeypatch,
         {"hook_event_name": "SessionStart", "session_id": "abc-123"}, str(sf))
    assert json.loads(sf.read_text())["claude_session_id"] == "abc-123"


def test_missing_session_id_does_not_clear_existing(tmp_path, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r", "claude_session_id": "abc-123"}))
    _run(monkeypatch, {"hook_event_name": "Stop"}, str(sf))
    assert json.loads(sf.read_text())["claude_session_id"] == "abc-123"


def test_user_prompt_submit_increments_message_count(tmp_path, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r"}))
    _run(monkeypatch, {"hook_event_name": "UserPromptSubmit", "prompt": "one"}, str(sf))
    assert json.loads(sf.read_text())["message_count"] == 1
    _run(monkeypatch, {"hook_event_name": "UserPromptSubmit", "prompt": "two"}, str(sf))
    assert json.loads(sf.read_text())["message_count"] == 2


def test_stop_does_not_increment_message_count(tmp_path, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r", "message_count": 3}))
    _run(monkeypatch, {"hook_event_name": "Stop"}, str(sf))
    assert json.loads(sf.read_text())["message_count"] == 3


def test_bad_stdin_does_not_raise(tmp_path, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r"}))
    monkeypatch.setenv("CRM_SESSION_STATUS_FILE", str(sf))
    monkeypatch.setattr("sys.stdin", io.StringIO("not json{"))
    session_hook.main([])  # must not raise; status file stays readable
    assert json.loads(sf.read_text())["repo"] == "r"


def test_session_start_clear_resets_stale_context_telemetry(tmp_path, monkeypatch):
    # A /clear within the SAME long-lived process doesn't re-exec start-session.sh (which
    # would otherwise reseed the file), so stale pre-clear telemetry must be dropped here.
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({
        "repo": "r", "message_count": 12, "context_pct": 91, "context_tokens_used": 180000,
        "context_window_size": 200000, "cost_usd": 3.5, "context_updated_at": 1.0,
    }))
    _run(monkeypatch, {"hook_event_name": "SessionStart", "source": "clear"}, str(sf))
    data = json.loads(sf.read_text())
    for key in ("message_count", "context_pct", "context_tokens_used", "context_window_size",
                "cost_usd", "context_updated_at"):
        assert key not in data
    assert data["status"] == "active"
    assert data["repo"] == "r"  # unrelated fields untouched


def test_session_start_compact_resets_stale_context_telemetry(tmp_path, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r", "message_count": 5, "context_pct": 88}))
    _run(monkeypatch, {"hook_event_name": "SessionStart", "source": "compact"}, str(sf))
    data = json.loads(sf.read_text())
    assert "message_count" not in data
    assert "context_pct" not in data


def test_session_start_resume_preserves_context_telemetry(tmp_path, monkeypatch):
    # resume continues prior context -- must NOT reset (unlike clear/compact).
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r", "message_count": 5, "context_pct": 60}))
    _run(monkeypatch, {"hook_event_name": "SessionStart", "source": "resume"}, str(sf))
    data = json.loads(sf.read_text())
    assert data["message_count"] == 5
    assert data["context_pct"] == 60


def test_session_start_startup_preserves_context_telemetry(tmp_path, monkeypatch):
    # startup is the normal fresh launch -- start-session.sh already reseeds the file with no
    # telemetry keys at all, so there is nothing to reset here; a leftover value (e.g. a reused
    # identity's stale file) should still be left alone, not actively wiped.
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r", "message_count": 5, "context_pct": 60}))
    _run(monkeypatch, {"hook_event_name": "SessionStart", "source": "startup"}, str(sf))
    data = json.loads(sf.read_text())
    assert data["message_count"] == 5
    assert data["context_pct"] == 60


def test_locked_status_file_serializes_concurrent_callers(tmp_path):
    import threading
    import time as time_mod

    path = str(tmp_path / "s.json")
    events = []

    def worker(label, hold_seconds):
        with session_hook.locked_status_file(path):
            events.append(f"{label}-start")
            time_mod.sleep(hold_seconds)
            events.append(f"{label}-end")

    t1 = threading.Thread(target=worker, args=("first", 0.05))
    t1.start()
    time_mod.sleep(0.01)  # ensure t1 has acquired the lock before t2 tries
    t2 = threading.Thread(target=worker, args=("second", 0))
    t2.start()
    t1.join()
    t2.join()

    # t2 must be blocked until t1 fully releases -- never interleaved.
    assert events == ["first-start", "first-end", "second-start", "second-end"]
