---
id: FMC-16
title: >-
  Fix launcher.py: unbounded subprocess output buffering, an incomplete
  process-group kill, and a startup race that can bypass the approval gate
status: Done
assignee:
  - '@claude'
created_date: '2026-07-21 14:44'
updated_date: '2026-07-21 22:20'
labels:
  - security
  - launcher
dependencies: []
references:
  - backlog/docs/reviews/doc-2 - Codex-full-codebase-review-2026-07-21.md
priority: high
type: bug
ordinal: 16000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by a second-opinion review (OpenAI Codex, gpt-5.6-sol, ultra effort) auditing the full codebase; each bug below was independently re-verified against the current code (file and line quoted, behavior traced through the actual call path) before being written up here, so this is confirmed, not a raw unreviewed LLM claim. All three bugs live in src/fast_mcp_claude/launcher.py, the headless fast-mcp-claude-launcher process: it long-polls the local server's inbox for tasks addressed to its launcher identity, spawns each as a claude -p subprocess in an allowlisted cwd, and (when the approval hook is armed) serves a per-task-gating approval relay over a unix domain socket that the spawned subprocess's PreToolUse hook talks to. This is the first task to cover launcher.py directly; none of the prior FMC tasks (auth.py, sandbox/body-size caps, store.py Notifier, tool-pattern conventions) touched this file's subprocess or relay lifecycle.

1. Unbounded subprocess output buffering (the function that spawns and awaits each claude -p subprocess, around lines 716-762). The subprocess is created with stdout and stderr both set to PIPE, with no byte cap on the reader, and the code awaits proc.communicate() (wrapped in asyncio.wait_for for the overall per-task timeout). asyncio.subprocess's communicate() is documented and implemented to read each stream to EOF and accumulate it fully in memory before returning; there is no streaming/backpressure path here. The only place output size is ever bounded is later, in the reply-building step that calls the _truncate_middle helper (introduced around line 487, used from around line 519 onward) to keep the JSON-encoded reply under the mesh's response size cap -- but that runs strictly AFTER communicate() has already returned, meaning the full stdout+stderr buffers already exist in process memory by the time any truncation logic runs. A single spawned task whose claude -p invocation (or a bug in the spawned agent's own tool use) emits a very large volume of stdout/stderr before exiting or before the overall timeout fires can grow the launcher process's memory without bound, risking an OOM that takes down the launcher and every other task it is concurrently running (max_concurrent tasks all share this one process), not just the offending task.

2. Incomplete process-group kill on timeout/shutdown (the group-kill helper, around lines 773-798, KILL_GRACE_S = 10.0). Each subprocess is spawned with start_new_session=True so it becomes its own process group leader (so os.killpg can reach any children it spawns). On timeout or shutdown, the helper correctly sends SIGTERM to the whole group via os.killpg, but the subsequent grace-period wait only calls lp.proc.wait() -- i.e. it waits on the group LEADER process object specifically, not on the group as a whole. If the leader exits within the grace period, the function returns immediately; the follow-up os.killpg(..., SIGKILL) is only reached in the except asyncio.TimeoutError branch, i.e. only when the leader itself fails to exit within the grace window. A grandchild process that ignores SIGTERM, or one that has re-parented itself out of the group (e.g. via double-fork or setsid), is never checked for and never receives the SIGKILL as long as the leader itself dies promptly -- it can keep running past both the per-task timeout that was supposed to bound its lifetime and the launcher's own shutdown sequence.

3. Unsupervised approval-relay startup race (launcher startup, around lines 1504-1535). When the approval hook is enabled, _serve starts two independent background tasks back to back with asyncio.create_task and no readiness handshake between them: the inbox-claiming bridge (which claims mesh tasks and immediately spawns gated claude -p subprocesses) and the approval-relay unix-socket server (which those subprocesses' PreToolUse hooks call out to for a per-tool-call decision). Nothing awaits confirmation that the relay's socket is actually bound and accepting connections (asyncio.start_unix_server, around line 1406, only logs "approval relay listening" after the fact) before the bridge is allowed to claim and spawn. Nor is either task's health checked again after startup -- both tasks are only awaited/joined in the final shutdown teardown (await stop.wait() then cancel both), so a mid-lifetime crash in the relay task goes completely undetected until the process exits. Per this project's own documented hook fallback (see hook.py's module docstring and the auto-pass code path around lines 76-81: an unreachable relay socket makes the hook fall back to permissionDecision "ask"), the intent is that Claude Code's own permission handling takes over safely when the relay can't be reached. But for a tool call that is already covered by the launcher's static --allowedTools grant for that task (built in _build_cmd, around lines 667-696), an "ask" decision from the hook does not act as an override-deny in this headless (-p, no TTY) context -- it simply leaves the pre-existing broad --allowedTools authorization to stand, so the intended per-call human approval gate is silently skipped for that call. This creates a real window (the socket not yet listening at startup, or the relay task having crashed later without anyone noticing) during which any already-allowlisted tool call runs completely ungated, even though the operator believes the approval relay is actively brokering every tool call. This is distinct from the existing "APPROVAL-GATE GUARD" fail-closed checks already in _serve (which only cover the hook binary being unresolvable on PATH, or the hook failing its own --settings self-test at startup) -- neither of those checks anything about the bridge/relay task ordering or ongoing relay-task health, so this gap is not already covered by that logic.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A spawned claude -p task that produces a very large volume of stdout and/or stderr no longer causes the launcher process's memory usage to grow unbounded while that subprocess is running or awaiting its timeout; output is bounded/streamed as it is produced rather than being fully accumulated in memory and only truncated after the fact, and other tasks running concurrently in the same launcher process are unaffected by one task's excessive output.
- [x] #2 When a spawned task's process group must be killed (per-task timeout or launcher shutdown), the kill sequence verifies that every process in that group has actually exited before considering the kill complete, not only the group leader; if any process in the group (including one that ignored SIGTERM or re-parented out of the group) is still alive after the SIGTERM grace period, a group-wide SIGKILL is sent so the group cannot outlive the intended per-task timeout or the launcher's own shutdown sequence.
- [x] #3 When the approval hook is enabled, the inbox-claiming bridge does not claim and spawn gated tasks until the approval relay's unix domain socket is confirmed open and accepting connections, so a hook call cannot fall back to an unsupervised "ask" decision (and thereby silently ride an existing --allowedTools grant) purely because the relay had not finished starting yet; additionally, if the relay task dies or exits at any point after startup, the launcher detects this condition (rather than continuing silently) and stops claiming new gated tasks until the relay is confirmed healthy again.
- [x] #4 All three fixes above are covered by automated tests: a test demonstrating bounded memory/output handling for a subprocess that produces oversized stdout/stderr, a test proving a SIGTERM-resistant or re-parented child in the same process group is force-killed within the grace period even when the group leader itself exits promptly, and a test proving the bridge cannot claim/spawn gated tasks before the approval relay is confirmed listening and that a relay-task crash after startup is detected rather than silently ignored.
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. AC#1 (unbounded output): replace proc.communicate() in _run_claude with two concurrent
   _read_capped() reader tasks (new helper, deque-based rolling TAIL window per stream,
   O(cap) memory) capped at a new MAX_SUBPROCESS_OUTPUT_BYTES module constant (4 MiB);
   drain them post-exit/post-kill via a new _drain_capped() bounded by _READ_DRAIN_TIMEOUT_S.
2. AC#2 (group kill): add _group_alive() (os.killpg(pgid, 0) existence probe) and rewrite
   _kill_group to poll the WHOLE group's liveness (not just lp.proc.wait() on the leader)
   for up to KILL_GRACE_S before SIGKILL.
3. AC#3 (relay startup race): give _approval_relay_server a `ready` Event set once the unix
   socket is bound+listening; add _run_approval_relay_supervised to run it under a
   restart-with-backoff supervisor that tracks a `relay_healthy` Event (set only while
   confirmed listening, cleared on any crash/exit); _bridge gains an optional
   relay_healthy param and a new _wait_for_relay_healthy_or_reconnect helper, gating
   claim/spawn both pre-poll and post-poll (mirroring the existing owner_confirmed
   dual-check pattern) -- deliberately no "give up" escape valve, unlike the owner-token
   gate, since AC#3's whole point is failing closed forever if the relay can't come up.
4. AC#4: regression tests in tests/test_launcher.py for all three, each confirmed via
   git stash to fail against pre-fix code (missing symbols / wrong arity) and pass post-fix.
5. Adversarial subagent review of the full branch diff before opening the PR (this
   campaign's established requirement for concurrency/security-boundary fixes).
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implemented and verified locally: full suite 373 passed (up from 367), ruff check clean, ruff format drift confirmed pre-existing (identical before/after via git stash) and untouched. All 6 new regression tests confirmed via git stash to fail against pre-fix launcher.py and pass post-fix.

Adversarial subagent review of the full branch diff (this campaign's established requirement for concurrency/security-boundary fixes) found 1 blocking + 1 test-quality issue, both fixed: (1) _group_alive only caught ProcessLookupError from os.killpg(pgid, 0), but the reviewer directly reproduced a PermissionError (EPERM) from that exact line under a pgid-recycling race during the polling window -- an uncaught exception that aborted _kill_group before its SIGKILL fallback ever ran, silently defeating AC#2's own guarantee. Fixed by catching (ProcessLookupError, PermissionError) in both _group_alive and the final SIGKILL call; added a dedicated regression test (test_group_alive_treats_permission_error_as_not_alive). (2) The new AC#2 regression test's timing budget (timeout_s=0.2/KILL_GRACE_S=0.3) was too tight for a fresh python3 interpreter to reach os.fork() before the launcher's own timeout fired, making the test flaky when run in isolation/cold-cache (reviewer reproduced 30/30 failures standalone despite passing within the full suite due to warm page cache) -- fixed by matching the proven-reliable budget of the sibling test_spawn_timeout_kills_group (timeout_s=0.5/KILL_GRACE_S=0.5); reverified 15/15 passes in isolation.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed all 3 launcher.py bugs (AC#1-3). AC#1: _run_claude now streams stdout/stderr through a new _read_capped() (deque-based rolling TAIL window, O(cap) memory) capped at MAX_SUBPROCESS_OUTPUT_BYTES=4MiB per stream, replacing proc.communicate()'s full-buffer read; drained post-exit via a bounded _drain_capped(). AC#2: _kill_group now polls group-wide liveness via a new _group_alive() (os.killpg(pgid,0) existence probe, treating both ProcessLookupError and PermissionError as 'gone') for up to KILL_GRACE_S instead of only awaiting the group leader, force-SIGKILLing the group if a grandchild (e.g. same-pgid double-fork survivor) is still alive. AC#3: _approval_relay_server gained a 'ready' Event set once its unix socket is confirmed bound+listening; a new _run_approval_relay_supervised() runs it under a restart-with-backoff supervisor tracking a 'relay_healthy' Event; _bridge gained an optional relay_healthy param gating gated-task claiming both pre-poll and in a post-poll residual recheck (mirroring the existing owner-token gate's dual-check pattern), with a dedicated _wait_for_relay_healthy_or_reconnect() helper that races reconnect_needed but deliberately has NO abandon-and-proceed escape valve (unlike the owner-token gate) since AC#3's entire purpose is failing closed forever on a persistently-unreachable relay. AC#4: added 8 regression tests (test_read_capped_keeps_tail_not_head, test_read_capped_returns_everything_under_cap, test_run_claude_bounds_stdout_stderr_below_cap, test_spawn_timeout_force_kills_lingering_group_member_after_leader_exits, test_group_alive_treats_permission_error_as_not_alive, test_bridge_blocks_then_resumes_claiming_on_relay_health, test_relay_supervisor_detects_crash_and_restarts), every one confirmed via git stash to fail against pre-fix launcher.py and pass post-fix. An adversarial subagent review of the full branch diff found and fixed 1 blocking bug (_group_alive's missing PermissionError handling, directly reproduced) and 1 flaky-test timing issue (fixed, reverified 15/15 stable in isolation) -- see implementation notes. Verified: full suite 374 passed (up from 367), uv run ruff check src/ tests/ clean; ruff format drift confirmed pre-existing (identical via git stash before/after this branch's changes) and untouched, same class already documented on FMC-4/6/8/9/11/12/13/14/15.
<!-- SECTION:FINAL_SUMMARY:END -->
