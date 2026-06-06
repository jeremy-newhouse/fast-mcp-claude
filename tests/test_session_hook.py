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


def test_bad_stdin_does_not_raise(tmp_path, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r"}))
    monkeypatch.setenv("CRM_SESSION_STATUS_FILE", str(sf))
    monkeypatch.setattr("sys.stdin", io.StringIO("not json{"))
    session_hook.main([])  # must not raise; status file stays readable
    assert json.loads(sf.read_text())["repo"] == "r"
