"""Unit tests for credential-helper clone argv construction (AC#3).

Asserts the token VALUE never appears in argv — only the mounted file path — so
it can't leak via process listings, and the clone URL is token-free.
"""

from __future__ import annotations

from sandbox_runner.gitcreds import clone_argv, token_present


def test_clone_argv_contains_no_token_value():
    argv = clone_argv(
        "https://github.com/owner/repo.git",
        "/work/repo",
        ref="main",
        token_path="/run/secrets/gh_token",
    )
    joined = " ".join(argv)
    # The secret path is referenced (helper cats it) but no token material is present.
    assert "/run/secrets/gh_token" in joined
    assert "credential.helper=" in joined
    # URL is passed verbatim, with no embedded credentials.
    assert "https://github.com/owner/repo.git" in argv
    assert "@github.com" not in joined
    assert "x-access-token:" not in joined


def test_clone_argv_shape():
    argv = clone_argv("https://x/y.git", "/dest", ref="dev", depth=1)
    assert argv[0] == "git"
    assert "clone" in argv
    assert argv[-2:] == ["https://x/y.git", "/dest"]
    assert "--branch" in argv and "dev" in argv
    assert "--depth" in argv and "1" in argv


def test_clone_argv_no_ref_no_depth():
    argv = clone_argv("https://x/y.git", "/dest", ref=None, depth=None)
    assert "--branch" not in argv
    assert "--depth" not in argv


def test_token_present(tmp_path):
    tok = tmp_path / "gh_token"
    assert token_present(str(tok)) is False
    tok.write_text("")
    assert token_present(str(tok)) is False  # empty file is "not present"
    tok.write_text("ghp_example")
    assert token_present(str(tok)) is True
