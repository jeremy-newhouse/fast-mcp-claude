# Handover — re-armed campaign, third pass: Codex CLI mesh support (FMC-17 → 18 → 19)

**Date**: 2026-08-03 (UTC; 2026-08-02 local — the tracker's session-17 log entry uses the local date) | **Grounded against**: `dev` @ `c8c550a`, working tree clean, 0 ahead / 0 behind `origin/dev` | **Tracker**: `doc-1`

## Paste-ready prompt for the next session

```
Run /backlog-handover restore in /Users/jdnewhouse/repos/fast-mcp-claude. Tracker: doc-1.
Cursor: FMC-17 — annotate the read-only mesh tools with MCP readOnlyHint so a Codex MCP
client can auto-approve just those instead of needing the blanket approve mode. Queue
order (FMC-17 → 18 → 19) confirmed by the user on 2026-08-02; do not re-ask.

Integration branch is dev, NOT main. The research backing all three issues is doc-3
(backlog/docs/research/) — read it before planning; its appendix has the exact
reproduction commands. Verify AC#3/#4 with real `codex exec` runs, never by code
inspection: codex-cli 0.145.0 is installed and the mesh server is live on
127.0.0.1:5473 under pm2. MCP_API_KEY is in .env — export it, never print it.
```

## State

| Item | Status |
| --- | --- |
| Branch / HEAD | `dev` @ `c8c550a`, clean, in sync with `origin/dev` |
| Cursor issue | **FMC-17** — To Do, unassigned, 5 ACs, no plan recorded yet (correct: the worker records the plan after picking it up) |
| Queue | FMC-17 (enhancement) → FMC-18 (feature, High) → FMC-19 (feature, depends on FMC-18) |
| Resolved so far | 15 issues across the first two passes — untouched by this session |
| Feature branches | none, local or remote |
| Open PRs | none |
| Tracker | `doc-1`, cursor + queue + session-17 log entry committed in `c8c550a` and pushed |
| Research doc | `doc-3` committed in `1545d59`; all three tasks link to it via their Documentation field |
| Handovers dir | this file only (`.claude/handovers/` is gitignored at .gitignore:87); 9 consumed handovers in `archive/handovers/` |

## Next steps

1. Preflight (clean tree, sync `dev`), then `git checkout -b feature/FMC-17 dev`.
2. `backlog instructions task-execution`; view FMC-17; mark In Progress + assign; record the implementation plan on the task.
3. Classify **every** tool in `src/fast_mcp_claude/tools/*.py` by whether it **writes to the store**, not by whether it blocks — AC#1 is explicit about this. Watch the traps: `wait_for_instruction` *claims* a message (mutating), `consume_interrupt` mutates, while `await_decision` / `wait_for_completion` / `subscribe` only read despite blocking. Likely read-only set: `who`, `list_messages`, `pending_approvals`, `get_status`, `list_files`, `read_file`, `subscribe`, plus the blocking-but-read-only waiters.
4. Declare `readOnlyHint` via FastMCP tool annotations on `@mcp.tool(...)`; leave mutating tools unannotated (AC#2).
5. Verify AC#3 **and** AC#4 with live `codex exec` runs using doc-3's appendix commands but with `default_tools_approval_mode="auto"`: a read-only tool must now succeed with no approval, and `write_file` must still raise one (`user cancelled MCP tool call` headlessly is the expected "still prompts" signal).
6. Document the annotation requirement for new tools in CLAUDE.md's tool-pattern section (AC#5).
7. On the branch: `backlog doc update doc-1` → cursor to FMC-18, move FMC-17 into Resolved, append the session-log entry.
8. Commit (`type(FMC-17): …` + `Refs: FMC-17` + the Co-Authored-By/Claude-Session trailers this repo uses), review the full diff (`/code-review`, or `/codex-review` for a second-model pass — note the irony that Codex reviewing Codex-support work is fine), fix findings, push, `gh pr create --base dev`, `gh pr merge --rebase --delete-branch`, sync `dev`, prune the local branch.

## Critical context / traps

- **Integration branch is `dev`.** `origin/HEAD` points at `main`, and `main` and `dev` are currently the *same commit*, but every one of this campaign's 16 sessions merged into `dev`. Do not open PRs against `main`.
- Export the bearer as `MCP_API_KEY="$(grep -E '^MCP_API_KEY=' .env | head -1 | cut -d= -f2-)"` and never echo it. Codex reads it via `bearer_token_env_var`, i.e. from its own process env.
- Codex verification runs bill the user's OpenAI account. Keep prompts tight and pass `-c 'model_reasoning_effort="low"'`; the probes in doc-3 cost ~3–30k tokens each.
- **Scope discipline:** the blanket `default_tools_approval_mode="approve"` is what works today, and writing the operator-facing config contract is **FMC-18's** job. FMC-17 is annotations + CLAUDE.md only. Do not pull FMC-18 work forward.
- `--dangerously-bypass-hook-trust` is only relevant to hook experiments (FMC-18/beyond), not to FMC-17.
- Backlog records change only through the `backlog` CLI. `doc update --content` **replaces** the whole body — read the file, edit surgically (the Resolved table holds 15 long rows worth preserving), then pass it back.

## Do not repeat

- **Don't put Codex hooks in `<repo>/.codex/hooks.json`** — verified NOT discovered on codex-cli 0.145.0 despite the published docs saying project-level discovery works. Only `$CODEX_HOME/hooks.json`, `config.toml [hooks]`, or `-c 'hooks.Stop=[...]'` fire. This cost a 5-minute hung run to discover.
- **Don't expect `auto` / `writes` / `prompt` to work headlessly against unannotated tools** — all three auto-deny with `user cancelled MCP tool call` before the request ever reaches the server. Only `approve` works pre-fix. That gap *is* FMC-17.
- **Don't pipe a long `codex exec` run through `tail`** — output buffers until EOF, so a killed run prints nothing and looks like a silent hang. Redirect to a file instead.
- `timeout` is not installed on this macOS host (no coreutils); use a background run plus a follow-up check instead.
