"""Tests for input validation and path sandboxing."""

import pytest

from fast_mcp_claude.errors import PermissionDeniedError, ValidationError
from fast_mcp_claude.utils.validation import (
    validate_channel,
    validate_message_id,
    validate_peer_name,
    validate_prompt,
    validate_session_id,
    validate_timeout,
    validate_workspace_path,
)


class TestSimpleValidators:
    def test_session_id_optional(self):
        assert validate_session_id(None) is None
        assert validate_session_id("") is None

    def test_session_id_valid(self):
        assert validate_session_id("abc_123") == "abc_123"
        assert validate_session_id("my.session-1") == "my.session-1"

    def test_session_id_invalid(self):
        with pytest.raises(ValidationError):
            validate_session_id("has space")
        with pytest.raises(ValidationError):
            validate_session_id("../escape")
        with pytest.raises(ValidationError):
            validate_session_id("a" * 200)

    def test_channel_valid_and_invalid(self):
        assert validate_channel("chat:room.1") == "chat:room.1"
        with pytest.raises(ValidationError):
            validate_channel("bad/slash")
        with pytest.raises(ValidationError):
            validate_channel("")

    def test_peer_name(self):
        assert validate_peer_name("laptop_42") == "laptop_42"
        with pytest.raises(ValidationError):
            validate_peer_name("has dot.")

    def test_message_id(self):
        assert validate_message_id("a" * 32) == "a" * 32
        with pytest.raises(ValidationError):
            validate_message_id("short")
        with pytest.raises(ValidationError):
            validate_message_id("z" * 32)  # non-hex

    def test_prompt(self):
        assert validate_prompt("hello") == "hello"
        with pytest.raises(ValidationError):
            validate_prompt("")
        with pytest.raises(ValidationError):
            validate_prompt("   ")

    def test_timeout(self):
        assert validate_timeout(None, default=10, cap=30) == 10
        assert validate_timeout(5, default=10, cap=30) == 5
        assert validate_timeout(100, default=10, cap=30) == 30  # capped
        with pytest.raises(ValidationError):
            validate_timeout(-1, default=10, cap=30)


class TestWorkspacePathSandbox:
    def test_rejects_when_no_roots_configured(self, tmp_path):
        with pytest.raises(PermissionDeniedError):
            validate_workspace_path(str(tmp_path / "x.txt"), workspace_roots=[])

    def test_allows_path_under_root(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hi")
        roots = [tmp_path.resolve()]
        result = validate_workspace_path(str(f), workspace_roots=roots, must_exist=True)
        assert result == f.resolve()

    def test_rejects_path_outside_roots(self, tmp_path):
        other_root = tmp_path / "allowed"
        other_root.mkdir()
        outside = tmp_path / "forbidden.txt"
        outside.write_text("hi")
        with pytest.raises(PermissionDeniedError):
            validate_workspace_path(str(outside), workspace_roots=[other_root.resolve()])

    def test_rejects_traversal_escape(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        attempt = root / ".." / "escape.txt"
        with pytest.raises(PermissionDeniedError):
            validate_workspace_path(str(attempt), workspace_roots=[root.resolve()])

    def test_rejects_symlink_escape(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "secrets.txt"
        outside.write_text("nope")
        link = root / "leak"
        link.symlink_to(outside)
        # After resolve(), `link` becomes `outside` — which is outside the root.
        with pytest.raises(PermissionDeniedError):
            validate_workspace_path(str(link), workspace_roots=[root.resolve()], must_exist=True)

    def test_rejects_relative_path(self, tmp_path):
        roots = [tmp_path.resolve()]
        with pytest.raises(ValidationError):
            validate_workspace_path("relative/file.txt", workspace_roots=roots)

    def test_rejects_null_byte(self, tmp_path):
        roots = [tmp_path.resolve()]
        with pytest.raises(ValidationError):
            validate_workspace_path("/tmp/has\x00null", workspace_roots=roots)

    def test_rejects_missing_file_when_required(self, tmp_path):
        roots = [tmp_path.resolve()]
        with pytest.raises(ValidationError):
            validate_workspace_path(str(tmp_path / "nope"), workspace_roots=roots, must_exist=True)
