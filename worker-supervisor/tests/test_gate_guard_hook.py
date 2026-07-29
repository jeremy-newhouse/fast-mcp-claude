"""ECA-140: the guard-hook runner must not execute a script outside `<repo>/.claude/hooks/`.

`_run_guard_hook` joined an UNVALIDATED control-socket string under the repo's hooks
directory and handed the result to `bash`. Two shapes reached execution, both
demonstrated against the unfixed code before a line was changed:

* `../../../outside/evil.sh` — the relative traversal the task named.
* `/abs/path/evil.sh` — a shape the task did NOT name and which needs no `..` at all:
  `Path.__truediv__` discards its left operand entirely when the right one is absolute,
  so `repo/".claude"/"hooks"/"/tmp/x.sh"` is simply `/tmp/x.sh`. This is why the fix is a
  charset predicate and not `".." not in script`.

**Every execution assertion here is a sentinel FILE, never a return value.** A decision
string tells you what the function concluded, not whether a subprocess ran — and the
pre-fix runs returned `("allow", "")` while the script executed, which is exactly the
combination a return-value assertion would have called safe. Each test therefore checks
the sentinel first; the refusal assertion is secondary and pins the REASON, so a future
change that stops executing for some unrelated reason still fails loudly.

What the resolve-and-contain check does and does not buy. It catches a hook FILE that is
a symlink out of the hooks directory, and it is indifferent to a symlinked `.claude` or
`hooks` directory (both sides are resolved). It does NOT catch a HARD link, which shares
an inode and has no "target" to resolve — the same limit ECA-135 recorded for `O_NOFOLLOW`.
Planting either requires write access to the operator's repo, which a lane with `Write`
already has under its own root, and such a lane can equally just overwrite a legitimate
hook's CONTENTS. So the containment check is about the control-socket string, not about
containing the lane; nothing here should be read as a lane-to-lane boundary, which this
daemon does not have (see `WorkerPolicy.mcp_servers`).

Falsification, measured rather than reasoned: with `is_safe_hook_script` stubbed to
`return True` and the resolve-and-contain block deleted, **8 of the 12 tests here fail and
4 pass**. Those 4 are exactly the positive and unchanged-behaviour cases, which SHOULD
survive a removed guard — a legitimate hook still runs, one under a symlinked hooks
directory still runs, a leading-underscore name still runs, and a missing hook still fails
open. Re-measure this pair if you add tests rather than adjusting it by arithmetic; a
first draft of this paragraph said 7/5 and put the predicate-divergence test among the
survivors, which the run disproved (stubbing the predicate breaks its hostile-input half).

Mutation coverage of the two guards, run separately: **10 mutations, 10 killed, 0
survived** — including moving the check to AFTER the join, comparing containment against
an UNresolved hooks_dir, `fullmatch` -> `match`, dropping the isinstance clause, and
restoring `WORKER_NAME_RE`'s leading character class. One honest caveat about the tenth:
dropping `".." not in script` is killed only by the explicit policy assertion below, not
by any traversal test, because the charset already rejects every separator. It is
belt-and-braces kept for symmetry with its two siblings, not a second line of defence.
"""

from __future__ import annotations

import os
from pathlib import Path

from claude_agent_sdk import PermissionResultDeny

from worker_supervisor.gate import QuestionBridge, WorkerPolicy, _run_guard_hook, make_gate
from worker_supervisor.names import is_safe_hook_script, is_safe_worker_name

# A guard that would be visible if it ran: it creates `sentinel` and then answers "allow",
# so a test that only looked at the decision would see the same result either way.
_SENTINEL_SCRIPT = "#!/usr/bin/env bash\ntouch {sentinel}\necho '{{}}'\n"


def _hooks(repo: Path) -> Path:
    d = repo / ".claude" / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _plant_outside(tmp_path: Path) -> tuple[Path, Path]:
    """A script OUTSIDE any repo, plus the sentinel path it would create if executed."""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    sentinel = outside / "EXECUTED"
    script = outside / "evil.sh"
    script.write_text(_SENTINEL_SCRIPT.format(sentinel=sentinel))
    return script, sentinel


def _gate(repo, policy, bridge, events):
    return make_gate(
        worker="w1",
        repo_root=repo,
        policy=policy,
        bridge=bridge,
        events=events,
        turn_id=1,
        question_timeout_s=1.0,
    )


async def test_relative_traversal_executes_nothing(repo, tmp_path):
    _hooks(repo)
    script, sentinel = _plant_outside(tmp_path)
    # Computed, not hand-written: the exact number of '..' segments that reaches the
    # planted script from the hooks directory, so the test cannot silently stop
    # traversing if the fixture layout changes.
    traversal = os.path.relpath(script, repo / ".claude" / "hooks")
    assert traversal.startswith(".."), traversal

    decision, reason = await _run_guard_hook(repo, traversal, "Bash", {"command": "echo hi"})

    assert not sentinel.exists(), f"guard hook OUTSIDE the repo executed via {traversal!r}"
    assert decision == "refused"
    assert "plain filename component" in reason


async def test_absolute_script_executes_nothing(repo, tmp_path):
    """The shape with no '..' in it — `Path.__truediv__` drops the left operand."""
    _hooks(repo)
    script, sentinel = _plant_outside(tmp_path)
    assert Path("/a/b") / str(script) == script, "pathlib no longer discards on absolute"

    decision, reason = await _run_guard_hook(repo, str(script), "Bash", {"command": "echo hi"})

    assert not sentinel.exists(), f"absolute guard-hook path executed: {script}"
    assert decision == "refused"


async def test_symlinked_hook_executes_nothing(repo, tmp_path):
    """A plain filename component that is a symlink OUT of the hooks directory."""
    hooks = _hooks(repo)
    script, sentinel = _plant_outside(tmp_path)
    (hooks / "innocent.sh").symlink_to(script)

    decision, reason = await _run_guard_hook(repo, "innocent.sh", "Bash", {"command": "echo hi"})

    assert not sentinel.exists(), "a symlinked hook executed its out-of-tree target"
    assert decision == "refused"
    assert "resolves outside" in reason


async def test_symlinked_hooks_directory_is_still_allowed(repo, tmp_path):
    """Both sides are resolved, so relocating `.claude/hooks` itself is NOT a refusal.

    The containment check is about the script string, not about where an operator keeps
    their hooks — a naive `hook_path.resolve()` compared against an UNresolved hooks_dir
    would refuse this legitimate layout.
    """
    real_hooks = tmp_path / "elsewhere-hooks"
    real_hooks.mkdir()
    sentinel = tmp_path / "RAN"
    (real_hooks / "ok.sh").write_text(_SENTINEL_SCRIPT.format(sentinel=sentinel))
    (repo / ".claude").mkdir()
    (repo / ".claude" / "hooks").symlink_to(real_hooks)

    decision, _ = await _run_guard_hook(repo, "ok.sh", "Bash", {"command": "echo hi"})

    assert sentinel.exists(), "a legitimate hook under a symlinked hooks dir did not run"
    assert decision == "allow"


async def test_non_str_script_is_refused_rather_than_crashing(repo):
    """Control-socket JSON is uncoerced: `guard_hooks={"Bash": 123}` really arrives here.

    Before the guard this raised TypeError out of `Path.__truediv__` INSIDE the SDK's
    permission callback — a crash in the one place ECA-135 established must never raise.
    """
    _hooks(repo)
    for value in (123, None, ["x.sh"], {"a": 1}, True):
        decision, reason = await _run_guard_hook(repo, value, "Bash", {"command": "echo hi"})
        assert decision == "refused", value
        assert "plain filename component" in reason


async def test_control_character_and_length_shapes_are_refused(repo):
    _hooks(repo)
    for value in ("", ".", "..", "ok.sh\n", "has space.sh", ".hidden.sh", "a" * 65, "sub/ok.sh"):
        decision, _ = await _run_guard_hook(repo, value, "Bash", {"command": "echo hi"})
        assert decision == "refused", value


async def test_legitimate_hook_still_runs(repo):
    hooks = _hooks(repo)
    sentinel = repo / "RAN"
    (hooks / "pre-review-test-check.sh").write_text(_SENTINEL_SCRIPT.format(sentinel=sentinel))

    decision, _ = await _run_guard_hook(
        repo, "pre-review-test-check.sh", "Bash", {"command": "echo hi"}
    )

    assert sentinel.exists(), "the fix stopped a legitimate hook from running"
    assert decision == "allow"


async def test_leading_underscore_hook_still_runs(repo):
    """`_common.sh` is the one real name that WORKER_NAME_RE would have refused."""
    hooks = _hooks(repo)
    sentinel = repo / "RAN"
    (hooks / "_common.sh").write_text(_SENTINEL_SCRIPT.format(sentinel=sentinel))

    decision, _ = await _run_guard_hook(repo, "_common.sh", "Bash", {"command": "echo hi"})

    assert sentinel.exists()
    assert decision == "allow"


def test_hook_and_worker_predicates_diverge_on_exactly_one_real_name():
    """Pins WHY there are two predicates, using the name that forced the split.

    Not a style preference: `_common.sh` exists in this operator's own `.claude/hooks`
    corpus, and reusing the worker predicate (which demands an alphanumeric first
    character) would have refused it — ECA-137's `_supervisor` finding, second edition.
    """
    assert is_safe_hook_script("_common.sh")
    assert not is_safe_worker_name("_common.sh")
    # Everything else stays in lockstep, including every traversal shape.
    for shared in ("check-bash.sh", "protect-files.sh", "gsd-read-guard.js", "a.b-c_d"):
        assert is_safe_hook_script(shared) and is_safe_worker_name(shared), shared
    for hostile in ("../x.sh", "/tmp/x.sh", "a/../b", "..", ".", "a..b"):
        assert not is_safe_hook_script(hostile) and not is_safe_worker_name(hostile), hostile


async def test_missing_hook_still_fails_open_unchanged(repo):
    """Pins the deliberate decision, so a later change to it has to be deliberate too.

    A well-formed hook that is simply absent keeps returning `none`, which the caller
    treats as allow. That is not the fail-open AC#2 is about: the caller who named the
    hook could have named none, so it crosses no boundary. The REFUSAL path denies.
    """
    _hooks(repo)
    decision, reason = await _run_guard_hook(repo, "not-there.sh", "Bash", {"command": "echo"})
    assert decision == "none"
    assert "missing" in reason


async def test_gate_denies_refusal_without_claiming_the_hook_ran(registry, events, repo, tmp_path):
    """End to end through `can_use_tool`: sentinel unwritten, event recorded, honest message.

    The message matters as much as the deny. Routing a refusal through the existing
    branch would have told the model "Denied by repo guard hook (deny)" about a hook that
    never executed — a false statement to the one reader who cannot check it.
    """
    _hooks(repo)
    script, sentinel = _plant_outside(tmp_path)
    traversal = os.path.relpath(script, repo / ".claude" / "hooks")
    policy = WorkerPolicy(allowed_tools=["Bash"], guard_hooks={"Bash": traversal})
    gate = _gate(repo, policy, QuestionBridge(registry, events), events)

    res = await gate("Bash", {"command": "echo hi"}, None)

    assert not sentinel.exists()
    assert isinstance(res, PermissionResultDeny)
    assert "Denied by worker policy" in res.message
    assert "repo guard hook" not in res.message, "claims a hook ran when none did"
    denials = [e for e in events.read("w1") if e["event"] == "tool_denied"]
    assert denials and denials[-1]["reason"].startswith("guard:refused:")


async def test_refusal_writes_no_file_named_after_the_script(registry, events, repo, tmp_path):
    """AC#2's second half: the refusal itself must not become the traversal.

    `script` reaches the refusal path as message text and as an event record BODY only.
    The event is keyed on the worker name, which ECA-137 validates inside `EventLog.path`,
    so a hostile script value cannot steer where the refusal is written.
    """
    _hooks(repo)
    target = tmp_path / "REFUSAL-LANDED-HERE"
    traversal = os.path.relpath(target, repo / ".claude" / "hooks")
    policy = WorkerPolicy(allowed_tools=["Bash"], guard_hooks={"Bash": traversal})
    gate = _gate(repo, policy, QuestionBridge(registry, events), events)

    res = await gate("Bash", {"command": "echo hi"}, None)

    assert isinstance(res, PermissionResultDeny)
    assert not target.exists(), "the refusal wrote through the very path it refused"
    assert list(events.read("w1")), "the refusal was silent"
