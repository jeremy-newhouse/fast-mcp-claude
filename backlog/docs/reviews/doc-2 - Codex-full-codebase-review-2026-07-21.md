---
id: doc-2
title: Codex full-codebase review (2026-07-21)
type: specification
created_date: '2026-07-21 14:28'
updated_date: '2026-07-21 14:28'
---
# Codex full-codebase review — fast-mcp-claude

**Reviewer:** OpenAI Codex (`gpt-5.6-sol`, effort `ultra`), freeform full-codebase audit (not a diff review) via the `codex-review` skill, scoped to `src/fast_mcp_claude/` and `tests/`.
**Adjudication:** every finding below was independently re-verified against the current code by dedicated verification passes (one per module group), each reading the real file/lines, quoting the actual code, and reproducing behavior where practical (including two live repros: the hook's `Client(headers=...)` TypeError, and the JSON-array-crashes-the-hook case). Verdicts are `CONFIRMED` / `PARTIALLY CONFIRMED` (real, but narrower/different than claimed) / `DISMISSED`.

**Headline result:** of ~38 discrete claims, essentially all were confirmed on current code, with only two narrowed in scope and a handful of mis-citations in one finding. This is an unusually high hit rate for a second-opinion pass — treat the findings below as real, not noise.

**Out of scope / not reviewed:** `worker-supervisor/`, `spawner/`, `sandbox-runner/`, `herdr-tmux-shim/` (separate subprojects). `.env` and other credential-shaped files were never opened.

---

## Top priority (read this section first)

1. **[CRITICAL] Forgeable admin authority in the channel permission relay** — any bearer-authenticated mesh peer can set `metadata.triggering_admin=true` on a prompt via `send_prompt`, and the channel sidecar's permission relay will auto-allow Bash/Edit/Write for that turn. There is no per-peer trust distinction — one shared `MCP_API_KEY` is the only authentication boundary. (`tools/messaging.py`, `channel.py:1423-1427`)
2. **The permission-relay's authenticated path is dead code today.** `hook.py` builds `Client(url, headers={...})` for authenticated relay calls, but the installed `fastmcp==3.4.4`'s `Client.__init__` has no `headers` kwarg (`auth=` is the real parameter) — this raises `TypeError` on every call, reproduced live. Whenever `MCP_API_KEY` is set (i.e. any real, non-toy deployment), the hook can never reach `request_approval`/`await_decision` at all — it always falls back to Claude Code's local `ask` UI. The controller-approval feature the whole hook exists for is silently inert for authenticated deployments.
3. **Server auth fails open, silently, on misconfiguration.** If `MCP_AUTH_ENABLED=true` (the default) but `MCP_API_KEY` is empty/unset, `server.py` logs a warning and serves every tool with `auth=None` — no startup failure. `__main__.py`'s own startup log can even claim `auth_enabled: true` in this state.
4. **Launcher reply/shutdown paths silently drop replies after reconnect.** A live task's reply keeps retrying against an already-closed `Client` after a reconnect (never redelivered), and `_shutdown()` similarly tries to list/reply through a client that `async with` already closed — defeating the "always reply" invariant. Duplicate-launcher ownership is also unenforced: a second instance reaps the real owner's in-flight tasks and keeps polling.
5. **Sandbox/CWD checks are classic TOCTOU** in both the file bridge (`tools/files.py`) and the launcher's CWD allowlist — canonicalize-then-later-open, with no fd-pinning or `O_NOFOLLOW`, so a local symlink-swap race can escape `WORKSPACE_ROOTS` or redirect a spawned task's cwd.

---

## Authentication, configuration, validation

- **[HIGH] CONFIRMED** — `server.py:26-35`: when `mcp_auth_enabled=True` but `mcp_api_key` is falsy, execution falls to an `else` branch that only logs a warning; `auth_provider` stays `None` and `FastMCP` serves unauthenticated. `__main__.py`'s `auth_enabled` log field (`mcp_api_key is not None and mcp_auth_enabled`) can read `True` for `MCP_API_KEY=""` while runtime is actually unauthenticated — a real logging/behavior mismatch on top of the fail-open bug.
- **[MEDIUM] CONFIRMED** — `auth.py:75-93`: the candidate token is compared *before* the lockout check, so a correct token always succeeds mid-lockout and every guess is still evaluated at request rate. The lockout only gates failure bookkeeping, not comparison — it doesn't throttle online guessing at all.
- **[MEDIUM] CONFIRMED** — `auth.py:37-45`: every request during lockout still emits a `logger.warning`, so an unauthenticated client can generate log I/O at request rate.
- **[MEDIUM] CONFIRMED** — `config.py:38-213`: no `Settings` field (ports, TTLs, poll intervals, byte budgets, concurrency) has range/`Field(ge=...)` validation. `mcp_port` flows unchecked into `mcp.run(port=...)` and fails only at socket-bind time.
- **[LOW] CONFIRMED** — `config.py:25-27,55`: API keys are plain `str`, no `SecretStr`, no entropy/blank check; incidental `repr()`/`str()` logging of a settings object would leak keys in full (redaction only scrubs known field *names*, see below).
- **[LOW] CONFIRMED** (reproduced) — `utils/validation.py:15-19,36-95`: all regexes end in `$` (not `\Z`/`fullmatch`) used with `.match()` — `"a"*32 + "\n"` passes `validate_message_id`.
- **[LOW] PARTIALLY CONFIRMED** — `utils/validation.py:121-130`: booleans coerce harmlessly; `float('inf')` actually clamps fine. The real bug is `NaN`: `nan < 0` is `False` and `min(nan, cap) == nan` in Python, so a NaN timeout slips past both the sign check and the cap uncapped and unrejected. Pydantic params carry no `allow_inf_nan=False`, so this is reachable from tool calls.
- **[LOW] CONFIRMED** — `logging_config.py`: sensitive-suffix list is `_password/_secret/_token/_credential` — no `_key`/`_api_key`, so a field literally named `remote_api_key` isn't redacted. Redaction also never recurses into nested dict/list values, and `record.getMessage()`/exception strings are never sanitized at all.

## File bridge (`tools/files.py`, `utils/validation.py`)

- **[HIGH] CONFIRMED** — `validate_workspace_path` canonicalizes once (`resolve(strict=False)` + `relative_to()`), and every caller (`list_files`, `read_file`, `write_file`) then performs a *separate, later* filesystem op on that path with no fd-based re-check or `O_NOFOLLOW`. A local symlink-swap between check and use escapes `WORKSPACE_ROOTS`. Note: canonicalization genuinely defeats naive `../` string-matching bypasses (the documented threat model) — it just isn't, and was never claimed to be, TOCTOU-proof.
- **[MEDIUM] CONFIRMED** — the 10MB cap is a `stat()` check followed by an independent `read_text()`; growth/replacement between the two bypasses the cap, and invalid UTF-8 triggers a full second read via `read_bytes()`.
- **[MEDIUM] CONFIRMED** — all file-bridge tools do synchronous `Path`/`os` I/O with zero `asyncio.to_thread`, blocking the whole event loop (including the `Notifier` machinery other tools depend on). `list_files` also `continue`s past its entry-count cap for hidden/unstatable entries, so only successfully-stat'd, non-hidden entries count toward the 1000-entry limit.
- **[MEDIUM] CONFIRMED** — `write_file(overwrite=False)` is a check-then-create race (no `O_CREAT|O_EXCL`); `overwrite=True` truncates in place with no temp-file+`os.replace`, so a failed write can destroy a previously-existing file.
- **[LOW] CONFIRMED** — valid-UTF-8 reads get newline normalization; the invalid-UTF-8 `errors="replace"` fallback reads raw bytes with no translation — inconsistent round-trip depending on decode success.

## SQLite store & concurrency (`services/store.py`)

- **[HIGH] CONFIRMED** — Teams/session-relay drains (`list_pending_teams_sends`/`list_pending_session_ops`) are plain reads with no claim step (unlike the atomic SELECT+UPDATE `pop_next_for_worker` uses); two concurrent hub drainers both perform the external side effect, and only the second `complete_*` call no-ops.
- **[HIGH] CONFIRMED** — `Notifier._get` unconditionally creates an `asyncio.Event` for any key, including for `timeout<=0` calls, and `forget()` is never invoked for `inbox:`/`pubsub:` keys (by design) nor for any key that never corresponds to a real row. An authenticated caller can create unboundedly many permanent dict entries via distinct nonexistent `channel`/`recipient_session` values with `timeout=0` — unbounded memory growth.
- **[MEDIUM] CONFIRMED** — in the retry loop, the fresh event is re-fetched *after* `check()` returns (not before, unlike the initial iteration) — a notify landing in that narrow window is swallowed, and the waiter sleeps until timeout despite genuinely new data existing.
- **[MEDIUM] CONFIRMED (bounded impact)** — cleanup marks messages expired with no accompanying `notify()` call; a blocked `wait_for_reply` waiter isn't woken immediately, though `wait_for_completion`'s direct DB fallback and the 300s timeout cap bound the actual delay.
- **[MEDIUM] CONFIRMED** — after a long gap (server downtime), a row can satisfy both the mark-expired and prune-delete conditions in the same sweep — goes QUEUED→EXPIRED→DELETED in one pass, so callers only ever see "not found," never a distinguishable "expired" state.
- **[MEDIUM] CONFIRMED** — approvals with `decision IS NULL` (abandoned) and the `interrupts` table are never touched by any TTL/cleanup logic anywhere in the file — permanent occupation of the oldest-first approval window, and stale interrupt flags can affect a future reused session_id.
- **[MEDIUM] CONFIRMED** — no code anywhere hardens the SQLite file/parent-dir permissions (`chmod`/`0600`/`0700`) — confidentiality of prompts/tool inputs/responses/`announce_token` depends entirely on ambient umask.
- **[LOW] CONFIRMED** — `json.dumps(x) if x else None` treats `{}` as falsy at 4 call sites — empty-dict metadata/payload round-trips to `None`.
- **Codex's own claim spot-checked: "all SQL parameterized, no injection path" — CONFIRMED.** The one dynamically-built query only concatenates static clause literals; all values flow through `?` placeholders.

## Messaging, permissions, presence, bulk APIs

- **[MEDIUM] CONFIRMED** — `get_status()` deserializes up to 1000 full message/approval rows (each up to several MB) merely to `len()` them, while the count still silently truncates at 1000 — should be `SELECT COUNT(*)`.
- **[MEDIUM] PARTIALLY CONFIRMED** — aggregate response size is real for `messaging.list_messages`/`permissions.pending_approvals` (up to ~100s of MB–1GB given per-row caps) and for `teams_outbox`/`session_relay` (smaller, ~12–200MB given a default limit of 50 with no caller-facing `limit` param). `pubsub.subscribe` is similarly capped low (~12.8MB max). `presence.list_presence` has **no SQL LIMIT at all** — unbounded in code, though naturally small in practice today. None of the six enforce an aggregate byte budget or pagination.
- **[MEDIUM] CONFIRMED** — presence redaction only recurses into nested `dict`s, not lists — `{"items":[{"api_key":"secret"}]}` leaks through `who()` verbatim; also no `_api_key` suffix in the sensitive-suffix list, so e.g. `peer_api_key` isn't caught either.
- **[LOW] CONFIRMED (narrow)** — a real race exists where the second, separate DB read after `Notifier.wait_for` times out can show a just-landed completion, but the tool hardcodes `ready: False` regardless.
- **[LOW] PARTIALLY CONFIRMED** — 2 of the 5 cited sites are real: `messaging.py`'s and `pubsub.py`'s `sender` fields have no validator at all (raw control characters pass through), and `list_messages`'s `limit` parsing isn't guarded like `pending_approvals`'s is (falls through to a generic `UNKNOWN_ERROR` instead of a field-specific validation error). The other 3 citations were mis-cited/duplicated from the `ready:false` finding above and don't show a distinct validation bypass.

## Channel sidecar (`channel.py`) — highest severity section

- **[CRITICAL] CONFIRMED** — `send_prompt`'s caller-supplied `metadata` has no key allowlist or origin check; it round-trips verbatim to the channel sidecar. `channel.py`'s permission handler auto-allows whenever `metadata.get("triggering_admin") is True`. Authentication is one shared bearer for the whole server — nothing distinguishes "the hub" from any other peer holding that same key. Any bearer-holder can address a prompt at a known channel identity (trivially discoverable via `who()`) with `triggering_admin: true` and get that turn's tool calls auto-allowed.
- **[CRITICAL] PARTIALLY CONFIRMED — narrower than "blanket bypass."** `inflight is None` does **not** auto-allow general Bash/Edit/Write (that case falls through to Claude Code's own local terminal dialog — a human still has to click allow). The real exposure is scoped to `send_teams`: it stamps `operator_direct: true` whenever `inflight is None` (one of two trust origins the hub honors), and FYI turns never set `inflight`, so processing an FYI's content (remote-originated) or landing in the post-clear/slow-turn window gets `send_teams` the same trust a genuine local operator prompt would get.
- **[HIGH] CONFIRMED** — the documented "two-part arming gate" is really one part in code: `enabled` is resolved purely from `--enabled`/`CHANNEL_ENABLED`/settings — nothing in `channel.py` ever reads back whether Claude Code actually loaded `--dangerously-load-development-channels`. The `initialized` readiness signal times out after 30s and the code proceeds anyway rather than gating loop-start on it.
- **[HIGH] CONFIRMED** — claimed-message state is race-prone in three ways: a failed mesh reply still advances the inbox loop as consumed; a late genuine reply can lose the timeout race, get bounced, and its content silently discarded; and `_push()`'s failure path sits outside the cleanup `try/finally`, so a broken stdio pipe leaves `inflight` permanently stuck on a stale message — during which any *unrelated*, truly-local permission request gets evaluated against that stale message's `triggering_admin` instead of hitting the local-dialog path.
- **[MEDIUM] CONFIRMED — and matters more than it sounds.** Disabled mode correctly starts no loops, but `reply`/`send_teams`/session-relay tools stay listed and callable regardless of `cfg.enabled`. A disabled sidecar's `reply` tool can still finalize an arbitrary caller-supplied `message_id` against the mesh if invoked directly — exactly the "adapter silently consumes a message meant for someone else" failure mode the architecture's disabled-invariant is meant to prevent, just reached via direct tool call instead of the (correctly inert) inbox loop.
- **[MEDIUM] CONFIRMED** — any non-`IDENTITY_LIVE_ELSEWHERE` announce rejection (validation error, malformed response, etc.) is only logged — it does not clear `announce_confirmed`, so the inbox loop can keep believing it owns an identity whose presence row was never actually refreshed.
- **Both of codex's "already holds" claims independently re-verified as CONFIRMED**: the disabled-loop invariant genuinely holds (unreachable `tg.start_soon` calls in the disabled branch), and non-admin approval failure/timeout paths all fall through to `"deny"`.

## Permission hook (`hook.py`)

- **[HIGH] CONFIRMED — live repro.** `Client(url, headers={...})` against installed `fastmcp==3.4.4` raises `TypeError: unexpected keyword argument 'headers'` (confirmed via `inspect.signature` and an actual run). Caught by the outer exception handler, so it does still fall back to `ask` (the "never silently deny" invariant technically holds) — but functionally, the entire authenticated-relay path is dead: it can never construct a client, so `request_approval`/`await_decision` are unreachable whenever `MCP_API_KEY` is set.
- **[MEDIUM] CONFIRMED — live repro.** Valid non-object JSON (`[]`, `null`, a bare number) parses fine but crashes at `event.get(...)` with an uncaught `AttributeError` (no top-level try/except around `main()`) — exits 1 with **no `hookSpecificOutput` emitted at all**, bypassing the documented ask-fallback for this specific input shape.
- Spot-check confirmed accurate (but distinct from the finding above): actual JSON decode errors, transport failures, timeouts, and invalid decision values all correctly fall back to `ask`.

## Launcher (`launcher.py`)

- **[HIGH] CONFIRMED** — duplicate-ownership refusal (`IDENTITY_LIVE_ELSEWHERE`) is only logged, never enforced; the reaper runs before any announce-confirmation check and unconditionally claims work — a duplicate instance reaps the real owner's in-flight tasks as lost and competes for new ones.
- **[HIGH] CONFIRMED** — on reconnect, in-flight `_handle_task` coroutines keep a closure reference to the now-closed client; their replies retry only the dead connection (3 attempts, then just logged) while the reaper explicitly skips them ("live in-flight tasks must not be reaped") — the reply is neither delivered nor reaped.
- **[HIGH] CONFIRMED** — on cancellation, `async with Client(...)` closes the connection before the outer `except CancelledError` handler's `_shutdown()` tries to list/reply through that same now-closed client; failures are swallowed, silently defeating "always reply on shutdown."
- **[HIGH] CONFIRMED** — subprocess stdout/stderr accumulate fully in memory via `communicate()` with no incremental cap; truncation only happens after the fact.
- **[HIGH] CONFIRMED** — `_kill_group`'s grace-period check only tracks the group leader's exit; a child that ignores SIGTERM (or re-parents) survives past both the per-task and shutdown kill paths since group-wide SIGKILL only fires if the *leader* itself doesn't exit in time.
- **[HIGH] CONFIRMED** — the approval-relay Unix socket server and the task-claiming bridge start as two unsynchronized tasks with no readiness handshake; a crash in the relay is unsupervised, and the hook's own documented fallback (`ask`) doesn't override an already-broad static `--allowedTools` grant.
- **[MEDIUM] CONFIRMED** — env sanitization is a denylist (`MCP_API_KEY` exact + `CRM_` prefix only); `PEERS` (containing every other peer's bearer token) and any cloud/VCS token or `SSH_AUTH_SOCK` in the parent env pass straight through to spawned workers.
- **[MEDIUM] CONFIRMED** — the "auth errors must never trip a reconnect" comment's intent is only wired into the heartbeat's own exception path, not the main bridge poll loop, which reconnects on the standard backoff for auth failures too — well within the window `AuthRateLimiter`'s 5-failures/300s lockout uses.
- **[MEDIUM] CONFIRMED** — CWD allowlist has the same TOCTOU shape as the file bridge (validate once in `parse_envelope`, spawn later with the raw path) — narrower window in practice, same architectural gap.
- **[MEDIUM] CONFIRMED** — `setting_sources` flows to `--setting-sources` with zero runtime validation; safety is pure operator convention ("KEEP `""` FOREVER" comment), not code enforcement — a nonempty value arms project hooks that bypass the tools ceiling entirely.
- **[MEDIUM] CONFIRMED** — a timed-out `--version` preflight probe is neither killed nor reaped; separately, the probed binary path isn't what's actually pinned for real task execution (both resolve `claude_bin` independently via `PATH`).
- **[MEDIUM] CONFIRMED — and now a stale TODO, not an inherent limitation.** Reaper/shutdown list calls omit `recipient_session` even though `messaging.list_messages` has supported that exact filter (backed by a dedicated index) since a later commit — the launcher's own "server-side filter is out of scope" comment predates that fix and was never updated to use it.
- **Spot-check of "otherwise correct" claims — CONFIRMED**: genuine argument-vector `create_subprocess_exec` everywhere (no shell/string concatenation), tools ceiling is a real subset check with correct empty-ceiling behavior, `--strict-mcp-config` always present, and zero subprocess/exec calls anywhere outside the sidecar files (confirmed via repo-wide grep).

## Session sidecars

- **[MEDIUM] CONFIRMED** — `_notify_macos` runs `subprocess.run(["osascript", ...], timeout=5)` synchronously inside the async bridge loop; a batch of new messages can block heartbeats/parent-death-detection for minutes.
- **[MEDIUM] CONFIRMED** — identity has no local format check before arming (silently invisible if malformed); non-finite/nonpositive poll or heartbeat intervals are unvalidated and can permanently suppress announces or corrupt the sleep scheduling.
- **[LOW] CONFIRMED** — malformed status-file field types (e.g. an int where a string is expected) raise inside the bridge's try block on every reconnect attempt, permanently blocking presence until the file is fixed.
- **[LOW] CONFIRMED** — `session_hook.py` has no lock/CAS between read and write of the shared status file; a slower `UserPromptSubmit` can overwrite a faster, later-finishing `Stop`'s write with stale data.
- Spot-check confirmed: notify-without-claim design genuinely holds, and `session_hook.py` is genuinely network-free (stdlib-only imports).

## Test coverage (verified directly)

- **CONFIRMED** — no `test_*.py` exists for `__main__.py`, `config.py`, `errors.py`, `hook.py`, `logging_config.py`, `server.py`, or `tools/files.py` (checked via `find tests -name "test_*.py"` against the module list — all seven are genuinely uncovered).
- **[LOW] CONFIRMED** — `tests/conftest.py`'s `settings_factory` builds `Settings(**defaults)` without `_env_file=None`; any `Settings` field not explicitly listed in `defaults` still sources from the developer's real `.env` on disk, making some test behavior environment-dependent.
- Codex's suggested priority regressions (forgeable admin provenance, duplicate-launcher/reconnect/shutdown replies, notifier lost-wakeup, concurrent drain races, symlink-swap races, fail-closed auth, stale approval/interrupt cleanup, nested presence redaction) all correspond to confirmed findings above and are reasonable places to start.

---

## Note on review integrity

One of the verification sub-agents (covering launcher env/sandbox findings) returned a harness-inserted banner indicating its output matched an "instruction-shaped pattern" and that control-like tags were neutralized before being surfaced. Nothing in that agent's actual returned findings (folded into the Launcher section above) reads as an injected instruction — it's a normal grounded finding report — but noting this here for transparency rather than silently passing it through.

## Suggested next steps

Given the volume, treat this as a backlog seed rather than one task: the five items in **Top priority** above are the ones worth scoping into actual Backlog issues first (especially #1/#2 — the admin-authority forgery and the dead authenticated-hook path are the two that most undermine the documented security model). The rest can be triaged into follow-up issues by module as capacity allows.
