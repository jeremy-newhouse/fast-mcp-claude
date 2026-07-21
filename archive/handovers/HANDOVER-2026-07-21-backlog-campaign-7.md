# Handover — resolve FMC-9 (channel.py forgeable triggering_admin/operator_direct trust stamps) — fifth issue of the 8-issue queue (FMC-9..16)

**Date**: 2026-07-21 | **Grounded against**: `dev` @ `1f4a425` (1 commit ahead of `origin/dev`, unpushed at write time — push it in restore's preflight), clean | **Tracker**: doc-1

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-9 -- channel.py has 2 CRITICAL trust-boundary bugs sharing one root
cause: the channel sidecar infers authority (admin, or "the local operator
personally typed this") from message-SHAPE signals rather than from anything
structurally tied to who actually originated the action, and every peer shares
one bearer key (MCP_API_KEY) so the receiving side has no independent way to
verify either inference. (1) _handle_permission (channel.py ~line 1425)
auto-allows a pushed tool call whenever the in-flight message's
metadata.triggering_admin is exactly true -- but that metadata comes from
send_prompt's caller-supplied metadata dict (tools/messaging.py), validated
only for JSON size, never for who's allowed to set it. Any bearer-authenticated
peer can send_prompt at a known channel identity (identities are discoverable
via who()) with metadata={"triggering_admin": true} and get every tool call in
that pushed turn auto-allowed with zero human confirmation. (2) _handle_send_teams
(channel.py ~line 1130) stamps operator_direct: true (one of exactly two trust
levels the hub honors for a Teams post) whenever rt.inflight is None -- but
"no in-flight message" doesn't reliably mean "the operator just typed this":
FYI-classified inbound pushes (metadata.expects_reply=false -- session-relay
notifications, broadcasts, late-reply push-backs) are deliberately delivered
WITHOUT setting rt.inflight (so an unanswered FYI can't wedge the mailbox for
channel_reply_timeout_s), and rt.inflight is unconditionally cleared in a
finally block even on the ambiguous/unknown consumption-verdict path where the
original turn might still genuinely be executing. Both windows let a
remote-originated action get treated as if the local human typed it. Queue
order (isolation/complexity-first: FMC-10 [done], FMC-14 [done], FMC-11 [done],
FMC-12 [done], FMC-9, FMC-13, FMC-15, FMC-16) confirmed by the user on
2026-07-21 -- do not re-ask before taking the next item.
```

## State
| Item | Status |
| --- | --- |
| Branch | `dev` @ `1f4a425`, clean |
| Sync with origin | 1 ahead / 0 behind `origin/dev` (this session's archive-handover commit not yet pushed — push it as part of restore's preflight, or it happens automatically if you push before starting FMC-9's branch) |
| Leftover `feature/*` branches (this campaign) | none (checked local + remote via `git branch -a` / `git ls-remote --heads origin feature/FMC-12` after merge — cleanly pruned) |
| Unrelated branches present | `chore/start-session-symlinkable`, `feat/eca-100-lane-reconfig`, `feat/eca-72-supervisor-pre-adoption`, `feat/session-to-session-messaging`, `feat/teams-relay`, `fix/teams-outbox-operator-direct` — all pre-existing, unrelated to this campaign's FMC- numbering; not campaign litter, do not touch. `feature/ECA-101`'s separate worktree (noted in prior handovers) was gone from `git worktree list`/`git branch -a` by this session's end — that concurrent session appears to have finished or been cleaned up; nothing to do. |
| Open PRs | none (`gh pr list --state open` empty) |
| Tracker cursor | FMC-9 (doc-1, updated and committed this session) |
| FMC-9 task status | To Do, unassigned, Priority High, Type bug |
| Queue after FMC-9 | FMC-13, FMC-15, FMC-16 (3 more after this one) |

## Next steps
1. Preflight per the skill: confirm `git status --porcelain` is clean and push the 1-ahead commit to `origin/dev` if not already done.
2. `git checkout -b feature/FMC-9 dev`.
3. Read `backlog instructions task-execution`; `backlog task view FMC-9 --plain` for the full self-contained description (already reviewed this session — see Critical context below for a summary); mark In Progress + assign; record implementation plan.
4. Read `src/fast_mcp_claude/channel.py` in full before touching anything — this file is large and both bugs are deeply tied to its runtime state machine (`rt.inflight`, the inbox loop, the FYI-vs-addressed classification, the permission-relay tee). Specifically: `_handle_permission` (~line 1417-1427, gate at ~1425), `_handle_send_teams` (~line 1123-1136), the inbox loop's FYI/addressed classification and `rt.inflight` set/clear logic (~lines 832-866), and the `finally` block that unconditionally clears `rt.inflight` including on the ambiguous/unknown consumption-verdict path.
5. Also read `src/fast_mcp_claude/tools/messaging.py`'s `send_prompt` (metadata is only size-validated, no allowlist) and `utils/validation.py`'s `validate_metadata`/`validate_json_object_size` (~lines 211-215) to confirm exactly what is and isn't currently checked on the metadata dict.
6. **This is the queue's most design-ambiguous item** (flagged as such since session 8's queue-ordering proposal, which the user confirmed). The task's own bugs share one root cause: authority is inferred from message SHAPE (a caller-supplied metadata key; the mere absence of in-flight state) rather than from anything structurally tied to who actually originated the action, and the single shared bearer key (`MCP_API_KEY`) gives the receiving side no independent way to verify either inference. You will need to invent an actual trust/provenance mechanism, not just patch the two specific symptoms — but scope it to what AC#1-3 actually require (see below), the same judgment call FMC-4/FMC-8/FMC-10/FMC-14/FMC-11/FMC-12 already made autonomously and documented. Do not gold-plate a whole new capability-token system if a narrower fix satisfies the ACs.
7. **Bug 1 (AC#1)**: `triggering_admin` must stop being just "whatever value the sender's `metadata` dict happened to contain." Candidate approaches (not prescriptive — read the actual hub-side code first, since the hub is what originally stamps `triggering_admin` for legitimate admin-triggered turns, per CLAUDE.md's Channel push flow section): the hub could sign/stamp `triggering_admin` in a way the channel sidecar can verify came from the hub itself (not from an arbitrary peer's `send_prompt` call), or `channel.py` could refuse to honor `triggering_admin=true` unless the message's `sender` matches a configured/known hub identity, or the trust decision could move server-side (the *server*, not the channel sidecar, decides whether a `send_prompt` caller is allowed to set `triggering_admin`, since the server already knows the caller's bearer identity in a way the channel sidecar's in-flight message metadata does not preserve). Whichever you pick, the AC's own scoping note matters: the no-in-flight-message case must keep falling through to the local terminal dialog unchanged — don't touch that path.
8. **Bug 2 (AC#2)**: `operator_direct: true` must not be inferred purely from `rt.inflight is None`. You need `_handle_send_teams` (or the state it reads) to distinguish "genuinely idle, human just typed this" from "processing an FYI with no in-flight slot" and "just cleared in-flight on an ambiguous verdict, original turn might still be running." This likely means adding explicit state (e.g. a flag set while an FYI is being acted on, and/or not immediately treating the post-ambiguous-clear window as safe — perhaps a short grace period, or an explicit "was this turn's outcome ambiguous" flag checked at send_teams time) rather than reusing `rt.inflight is None` as a proxy for both "idle" and "safe to trust as operator-direct." Read the FYI classification code (`metadata.expects_reply=false`) and the ambiguous/unknown consumption-verdict branch carefully — they're two distinct windows, both need closing.
9. Add regression tests (AC#3): one demonstrating an addressed `send_prompt` with `metadata.triggering_admin=true` from an untrusted caller does NOT result in the permission relay auto-allowing a subsequent tool call (must fail pre-fix, pass post-fix); one demonstrating a `send_teams` call made during FYI processing, or in the post-ambiguous-clear window, does NOT receive `operator_direct` trust (same fail-pre/pass-post requirement). Per the FMC-14 lesson (see Do not repeat below), assert the *specific* trust-level/behavior distinction, not a bare exception or truthy check.
10. Read `backlog instructions task-finalization` before checking any AC. Verify AC#1-3 with objective evidence.
11. Update the tracker (doc-1) on the branch: move FMC-9 to Resolved, advance cursor to FMC-13, append the session-13 log entry.
12. Commit, review (self or adversarial subagent — strongly lean toward the adversarial subagent again; this is a security-boundary/trust-model fix, the exact class where FMC-11's and FMC-12's adversarial reviews each caught a real bug), push, open+merge PR into `dev` via `gh pr merge --rebase --delete-branch`, sync local `dev`, delete local branch, verify remote deletion with `git ls-remote --heads origin feature/FMC-9`.
13. Archive this handover to `archive/handovers/` (check for name collisions — `-2.md` through `-6.md` already exist from earlier sessions, so this becomes `-7.md`), commit, write the session-13 handover for cursor FMC-13, push `dev`.

## Critical context / traps
- **This campaign's tracker is doc-1, not doc-2.** doc-2 is the Codex full-codebase review report FMC-9..16 were generated from — read it only if FMC-9's own self-contained description isn't enough.
- **FMC-9 and FMC-13 share `channel.py`** — the queue deliberately sequences them back-to-back to reduce rebase churn. Whatever trust/provenance mechanism you design for FMC-9 may well be directly relevant to FMC-13's bugs too (also in `channel.py`'s permission/arming/state-race territory) — read FMC-13's task description before finalizing FMC-9's design, in case a slightly broader (but still scoped) fix now saves rework next session. Don't over-build for FMC-13 speculatively, but don't paint yourself into a corner either.
- **This is explicitly a bugfix task, not a request to redesign the trust model from scratch.** The task's acceptance criteria are narrow and behavioral (AC#1-3 above) — satisfy those without inventing unnecessary new abstractions (e.g. a full capability-token system) unless the narrow fixes genuinely require it.
- **FMC-12 (just resolved this session) is in the same file's neighborhood conceptually** (both are about trust boundaries around message metadata / in-flight state) but is a fully separate, already-merged fix in `services/store.py` (atomic hub-drain claims + bounded Notifier growth) — no code overlap, just thematic proximity. Don't conflate the two.
- **From FMC-14's session (session 10)**: an adversarial subagent review caught a real bug in a new test file — a bare `pytest.raises(Exception)` regression test that would also pass against the very bug it was meant to catch. Always assert the *specific* exception type/attribute/state transition (or here: the *specific* trust-level outcome) a fix's regression test is meant to distinguish, not a bare truthy/exception check.
- **From FMC-11's and FMC-12's sessions**: adversarial subagent review of a security-boundary/concurrency fix caught a real bug both times (a discarded `os.write()` return value in FMC-11; a waiter-refcount-vs-eviction ordering gap in FMC-12, closed with a dedicated regression test the same session it was found). Strongly worth running an adversarial review again for FMC-9's trust-model fix — this exact review step has a 100% hit rate on the last two security/concurrency fixes in this campaign.
- **`gh pr merge --delete-branch` works cleanly when the tree is clean at merge time** (confirmed again in FMC-12's session: both local and remote branches were correctly removed, and it auto-switched off the feature branch and fast-forwarded local `dev` automatically — `git checkout dev && git pull --ff-only` reported "Already up to date"). The remote-tracking ref for the deleted branch can linger in `git branch -a` until a `git fetch --prune` — that's a stale local cache, not an actual leftover branch; `git ls-remote --heads origin feature/<KEY>` is the authoritative check.
- **`feature/ECA-101`'s worktree** (noted as unrelated concurrent-session litter in several prior handovers) was gone by the end of this session — nothing to do, just don't be surprised it's no longer there.

## Do not repeat
- Don't re-run `/backlog-handover init` when a tracker/queue/cursor/handover already exist and are current — this is the established norm for this campaign (confirmed explicitly in session 9).
- Don't assume a library's/framework's API shape from memory or general docs — verify against the actual installed/available version/platform before choosing an approach. FMC-14 (fastmcp Client API), FMC-11 (`os.scandir` dir_fd support), and FMC-12 (`_db_lock`'s actual scope, verified by reading it before designing the concurrency test) all hit this discipline; the same applies here if you reach for anything in `channel.py`'s MCP SDK usage (e.g. the raw-stdio-tee permission-relay mechanism CLAUDE.md documents as a workaround for an SDK gap) that you haven't directly confirmed still behaves as documented.
- Don't write a regression test with a bare `pytest.raises(Exception)` (or equivalent loose assertion, e.g. just checking a response is truthy/falsy) when the whole point is distinguishing "behaves correctly for the right reason" from "happens to also pass for an unrelated reason." FMC-14's adversarial review caught exactly this.
- Don't trust a single self-review as sufficient for a trust-model/security-boundary fix — FMC-11's and FMC-12's adversarial subagent reviews each found a real bug the self-review missed. Budget for that extra pass.
