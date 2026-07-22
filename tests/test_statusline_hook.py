"""Tests for fast-mcp-claude-statusline-hook — the context/cost telemetry writer.

It reads the CC statusLine JSON on stdin, merges context/cost fields into the status file, and
always prints a status line (fallback text on any error, since stdout IS what the operator sees).
"""

import io
import json

from fast_mcp_claude import statusline_hook


def _run(capsys, monkeypatch, payload, status_path: str | None):
    if status_path is None:
        monkeypatch.delenv("CRM_SESSION_STATUS_FILE", raising=False)
    else:
        monkeypatch.setenv("CRM_SESSION_STATUS_FILE", status_path)
    stdin = json.dumps(payload) if not isinstance(payload, str) else payload
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    statusline_hook.main([])
    return capsys.readouterr().out.strip()


def _payload(pct=42, tokens=15500, window=200000, cost=1.2345, model="Sonnet"):
    return {
        "model": {"display_name": model},
        "context_window": {
            "used_percentage": pct,
            "total_input_tokens": tokens,
            "context_window_size": window,
        },
        "cost": {"total_cost_usd": cost},
    }


def test_no_status_file_env_still_prints_line(capsys, monkeypatch):
    line = _run(capsys, monkeypatch, _payload(), None)
    assert "42% context" in line
    assert "$1.23" in line


def test_merges_context_and_cost_into_status_file(tmp_path, capsys, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"identity": "mini2.r", "repo": "r"}))
    _run(capsys, monkeypatch, _payload(pct=80, tokens=160000, window=200000, cost=3.5), str(sf))
    data = json.loads(sf.read_text())
    assert data["context_pct"] == 80
    assert data["context_tokens_used"] == 160000
    assert data["context_window_size"] == 200000
    assert data["cost_usd"] == 3.5
    assert "context_updated_at" in data
    assert data["repo"] == "r"  # static fields preserved


def test_status_line_includes_model_pct_and_cost(capsys, monkeypatch):
    line = _run(capsys, monkeypatch, _payload(pct=17, cost=0.05, model="Opus"), None)
    assert line == "Opus · 17% context · $0.05"


def test_missing_context_window_falls_back_gracefully(tmp_path, capsys, monkeypatch):
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r"}))
    line = _run(capsys, monkeypatch, {"model": {"display_name": "Sonnet"}}, str(sf))
    assert line == "Sonnet"
    data = json.loads(sf.read_text())
    assert "context_pct" not in data  # no spurious fields written


def test_bad_stdin_prints_fallback_line_not_raise(capsys, monkeypatch):
    monkeypatch.delenv("CRM_SESSION_STATUS_FILE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("not json{"))
    statusline_hook.main([])
    assert capsys.readouterr().out.strip() == "claude"


def test_null_used_percentage_omitted_from_line(capsys, monkeypatch):
    # used_percentage is null before the first API call / right after /compact (per docs).
    payload = {"model": {"display_name": "Sonnet"}, "context_window": {"used_percentage": None}}
    line = _run(capsys, monkeypatch, payload, None)
    assert line == "Sonnet"


def test_closed_stdin_still_prints_fallback_line(capsys, monkeypatch):
    # sys.stdin=None (e.g. a closed fd) makes json.load raise AttributeError, not
    # JSONDecodeError/ValueError -- must still be caught, not escape main() and print nothing.
    monkeypatch.delenv("CRM_SESSION_STATUS_FILE", raising=False)
    monkeypatch.setattr("sys.stdin", None)
    statusline_hook.main([])
    assert capsys.readouterr().out.strip() == "claude"


def test_non_numeric_cost_does_not_block_context_pct_write(tmp_path, capsys, monkeypatch):
    # A malformed cost field must not prevent context_pct from reaching the status file, and
    # must not blow up _status_line's `:.2f` format.
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r"}))
    payload = {
        "model": {"display_name": "Sonnet"},
        "context_window": {"used_percentage": 55},
        "cost": {"total_cost_usd": "not-a-number"},
    }
    line = _run(capsys, monkeypatch, payload, str(sf))
    assert line == "Sonnet · 55% context"  # no cost segment, but not blank/fallback either
    data = json.loads(sf.read_text())
    assert data["context_pct"] == 55  # the write still happened
    assert "cost_usd" not in data


def test_non_dict_cost_does_not_raise(capsys, monkeypatch):
    payload = {"model": {"display_name": "Sonnet"}, "cost": "not-a-dict"}
    line = _run(capsys, monkeypatch, payload, None)
    assert line == "Sonnet"


def test_concurrent_hooks_do_not_lose_each_others_update(tmp_path):
    # Models the real race: session_hook.py (Stop -> status/updated_at) and statusline_hook.py
    # (context_pct/cost_usd) both do a load-mutate-write cycle on the SAME file. Without
    # locked_status_file wrapping both, a lost update reverts whichever side wrote first.
    import threading
    import time as time_mod

    from fast_mcp_claude import session_hook

    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"repo": "r", "status": "working"}))

    def session_side():
        with session_hook.locked_status_file(str(sf)):
            st = session_hook.load_status_file(str(sf))
            time_mod.sleep(0.05)  # widen the race window
            st["status"] = "idle"
            session_hook.atomic_write_status_file(str(sf), st)

    def statusline_side():
        time_mod.sleep(0.01)  # start just after session_side acquires the lock
        with session_hook.locked_status_file(str(sf)):
            st = session_hook.load_status_file(str(sf))
            st["context_pct"] = 42
            session_hook.atomic_write_status_file(str(sf), st)

    t1 = threading.Thread(target=session_side)
    t2 = threading.Thread(target=statusline_side)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    data = json.loads(sf.read_text())
    assert data["status"] == "idle"  # session_side's write survived
    assert data["context_pct"] == 42  # statusline_side's write survived too -- neither lost
