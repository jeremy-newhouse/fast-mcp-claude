---
name: codex-review
description: >-
  Get a second-opinion code review from OpenAI Codex (gpt-5.6-sol; xhigh reasoning by
  default, ultra for high-stakes) — a different model family than Claude, with
  different blind spots — on local changes via the `codex` CLI. Use this whenever the
  user wants Codex eyes on code: "codex review", "ask codex", "get a second opinion
  from codex / gpt-5.6", "cross-check this diff with codex", "what does codex think of
  this", a second-model pass before a merge, or any time they want an independent model
  to review a branch, uncommitted changes, a specific commit, or a design concern —
  even if they don't say the exact words "codex-review". Runs `codex exec review`
  (git-aware: --base / --uncommitted / --commit) or a read-only freeform consult,
  captures the output, and triages every finding against the real code before reporting
  — Codex is a second opinion, not the verdict. NOT for Claude's own review (use
  /code-review or /review max for that), and NOT for running Codex as an autonomous
  editor — this skill is read-only, review-only.
---

# Codex review (second-opinion model pass)

Consult **OpenAI Codex (`gpt-5.6-sol`)** as an independent reviewer over local changes. The
value is the _different model family_: gpt-5.6-sol misses and catches different things than
Claude, so a Codex pass surfaces issues a Claude-only review (`/code-review`,
`/review max`) won't — and vice-versa. Treat its findings as claims you **adjudicate**,
not output you relay.

## Prerequisites (check fast, fail clearly)

- `codex` CLI on PATH and authenticated — `codex login status` should say "Logged in".
  If not, tell the user to run `! codex login` (interactive) and stop.
- **`codex-cli >= 0.144.1`** — the default `gpt-5.6-*` models 400 on older CLIs
  ("model requires a newer version of Codex"). The wrapper preflights this and fails
  fast with the upgrade command (`npm i -g @openai/codex@latest`); upgrade every nvm
  node you run codex under, not just one.
- Config defaults (`~/.codex/config.toml`) are `model = "gpt-5.5"`; the wrapper passes
  model/effort explicitly so the skill is robust to config drift.
- **Data egress + secrets — read before running.** `codex exec` ships the reviewed code
  to OpenAI under the user's ChatGPT login. Two consequences:
  - `codex exec review --uncommitted` includes **untracked** files in the diff. A local
    secret that isn't gitignored (stray `.env`, dump, key) would be sent. Glance at
    `git status` first; don't review a tree with untracked secrets.
  - In a freeform `--prompt`, **scrub secrets/PII** (keys, tokens, passwords, client
    data) before pasting, and keep it under ~100K chars. Our own repo code is fine;
    confidential client data is not.

## Run it

Use the bundled wrapper — it captures Codex's final message to a temp file and prints
the path as `CODEX_REVIEW_OUTPUT=<path>` on the last line. A review takes **1–5 min**;
for anything slow, run it in the background and poll rather than blocking the turn.

```bash
bash .claude/skills/codex-review/scripts/codex-review.sh <mode> [-C <repo>] [-e <effort>] [-t <secs>]
```

Pick the selector by what the user is reviewing:

| Situation                                | Command                                                 |
| ---------------------------------------- | ------------------------------------------------------- |
| Work not yet committed                   | `codex-review.sh --uncommitted -C <repo>`               |
| A whole feature branch                   | `codex-review.sh --base dev -C <repo>`                  |
| One specific commit                      | `codex-review.sh --commit <sha> -C <repo>`              |
| A design / conceptual question (no diff) | `codex-review.sh --prompt - -C <repo>` (heredoc, below) |

`-C <repo>` must be the **repo root or worktree** holding the change — evolv-ultra is
three repos (`evolv-ultra`, `evolv-ultra-be`, `evolv-ultra-fe`), and BE/FE work usually
lives in a worktree under `/Volumes/_repos/worktrees/`. Resolve where the diff actually
is (check the branch/worktree); don't default to the cwd root repo. `dev` is the base
branch. If a change spans repos, **review each affected repo** (see Cross-repo below).

### Cross-repo: all three repos, not just evolv-ultra

A review must not be blind to the sibling repos. Two facts shape how:

- **codex can already read all three repos.** Its read-only sandbox has full-disk read
  (verified), so from any one repo it can open files in the other two — the wrapper
  resolves the sibling checkouts (worktree-safe) and `--prompt` mode **auto-prepends a
  cross-repo note** (the three paths + the FE-vendors-BE-`openapi.json`/`tool-catalog.json`
  coupling). Pass `--solo` to suppress it for a strictly single-repo consult.
- **`codex exec review` is git-scoped to one repo and takes no context** (no `-C`, no
  `--add-dir`, no prompt — verified). So a `--base`/`--uncommitted`/`--commit` review
  sees only that repo's diff. The wrapper prints a stderr reminder after each one.

So for cross-repo surface — a BE response-schema / route / tool-definition change that the
FE vendors — don't rely on one `exec review`. Either:

1. Run a focused `exec review` in **each** affected repo (`-C` each), then adjudicate
   both, **or**
2. Run a `--prompt` cross-repo contract pass: paste the BE diff (`git -C <be> diff
origin/dev...HEAD`) and ask codex to check it against the FE consumer — the sibling
   paths are already in context, so it can open the FE types/callers itself.

> Durable native option (optional, bigger footprint): a committed `AGENTS.md` at each
> repo root is codex's auto-loaded project doc and would reach every worktree branched
> from `dev` — but it touches three product repos (PR/CI per repo) and `exec review`'s
> rigid output contract makes its effect on findings unreliable. The skill-local
> injection above needs no repo changes. Raise the `AGENTS.md` route only if the team
> wants codex (review _and_ interactive) cross-repo-aware everywhere.

### Model / effort ladder

Default is `gpt-5.6-sol` + `xhigh` (set in the wrapper). Override with `-e` / `-m` per the
ladder — escalate deliberately, don't reflex to ultra:

| Situation                                                              | Model           | `-e`     |
| ---------------------------------------------------------------------- | --------------- | -------- |
| Cheap triage — narrow, low-stakes, small diff                          | `gpt-5.6-terra` | `medium` |
| **Default review** (most cases)                                        | `gpt-5.6-sol`   | `xhigh`  |
| High-stakes / cross-cutting / pre-merge-to-`main` / security-sensitive | `gpt-5.6-sol`   | `ultra`  |

Use `ultra` only when the change is materially ambiguous, expensive to reverse, or
cross-cutting — or when an `xhigh` run came back shallow.

### Raw commands (when you need to adapt)

The wrapper is the common path; construct the call directly when you need flags it
doesn't expose (`--title`, `--json`, `--output-schema`, images, `-p <profile>`):

```bash
# git-aware review — read-only by design; review has no -C, so cd into the repo first
cd <repo> && codex exec review --base dev -m gpt-5.6-sol -c model_reasoning_effort=xhigh -o /tmp/cr.md
```

For a freeform/conceptual review (no git diff), pipe a structured prompt via heredoc —
robust for long, code-heavy context where argv would break. `-s read-only` keeps Codex
from mutating anything. Note this raw form **skips the wrapper's cross-repo injection**;
prefer `codex-review.sh --prompt -` so the sibling paths are auto-prepended, or name the
sibling repo roots in `### CONTEXT` yourself:

```bash
cat << 'END_PROMPT' | codex exec -s read-only -m gpt-5.6-sol -c model_reasoning_effort=xhigh -C <repo> --skip-git-repo-check -o /tmp/cr.md
### ROLE
You are a senior <domain> reviewer.
### TASK
Review the following for <correctness | security | concurrency | API design>.
### CONTEXT
<paste the relevant code / diff / constraints — sanitized, < ~100K chars>
### DESIRED OUTPUT
Severity-tagged findings as `file:line — issue — fix`, then a one-line verdict.
END_PROMPT
```

**Deliberate deviation from askcodex:** that skill uses `--sandbox workspace-write`
because it's a _coding_ consult. A review must never mutate files, so this skill is
**read-only** — `exec review` has no sandbox flag (can't edit), and the freeform path
forces `-s read-only`.

## After Codex responds: adjudicate, don't relay

Read `CODEX_REVIEW_OUTPUT` and treat each finding as a _claim to verify_:

1. **Re-ground against the real code.** Codex reads the **local checkout**, which can lag
   `origin/dev` — the same stale-checkout trap our verify agents hit. Open the cited
   `file:line` and confirm the issue exists on current code before repeating it.
2. **Confirm or dismiss each finding.** Keep what you can see is real; drop
   hallucinations, stale hits, and style nits that contradict project standards. State
   _why_ you dismissed each — that adjudication is the signal the user wants.
3. **De-dupe vs. Claude's own review.** If a Claude pass (`/code-review`) already ran,
   flag where the two models **agree** (highest confidence) and where only Codex raised
   something (worth a look).

## Report like this

```
## Codex review — <repo> <selector>  (gpt-5.6-sol, <effort>)

**Confirmed** (verified against current code)
- [SEV] file.py:42 — <issue>. Fix: <one line>.

**Dismissed** (Codex raised, not real)
- file.ts:10 — <why: stale / hallucinated / contradicts standard X>.

**Verdict:** <ship / fix-first / needs-discussion>.  Agreement with Claude review: <…>.
Raw: <CODEX_REVIEW_OUTPUT path>
```

Lead with what's actionable. Two model families agreeing on a finding is the strongest
signal in the report — say so up front.

## When it fails

- **Auth (exit non-zero, 401/403):** `codex login status`; if logged out, have the user
  run `! codex login`, then retry.
- **Transient (network/5xx):** retry once. If it still fails, report it — don't loop.
- **Bad model (400):** drop `-m` to fall back to the config default, note the swap.
- **Timeout (exit 124):** the wrapper reports it — raise `-t`, narrow the selector, or
  drop to `-e medium`/`-m gpt-5.6-terra`.

## Cautions

- **Second opinion, not authority.** You adjudicate; never auto-apply Codex's edits.
- **Read-only always.** No `workspace-write` / `danger-full-access` for a review.
- **Backgrounding:** if you fan out several reviews with `&`, `wait` for all before
  interpreting, so no run's output is half-written.
