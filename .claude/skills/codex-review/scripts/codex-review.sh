#!/usr/bin/env bash
# codex-review.sh — OpenAI Codex (gpt-5.6-sol) second-opinion code review with output capture.
#
# Wraps the fiddly bits (selector flags, model/effort, timeout, final-message capture)
# so every invocation is identical. Streams Codex's review to stdout AND writes the
# final message to a temp file, printing its path on the last line as:
#     CODEX_REVIEW_OUTPUT=<path>
#
# The caller (Claude) reads that file, triages the findings against the real code,
# and presents a deduped, adjudicated summary — Codex is a second opinion, not the verdict.
#
# review modes use `codex exec review` (git-aware; read-only by design — no sandbox flag,
# no -C, so we cd into the repo). prompt mode uses `codex exec -s read-only` for a
# freeform consult that is NOT a git diff (a design question, a single-file concern).
#
# Cross-repo: evolv Ultra is three coupled repos (root, -be, -fe). read-only codex has
# full-disk read (verified), so it CAN read all three from any one — the blindness is
# that a review is never TOLD the siblings exist. prompt mode auto-prepends a sibling-
# context note (paths + the FE<-BE openapi/tool-catalog coupling) so a consult isn't
# blind; `--solo` suppresses it. `exec review` accepts no prompt / -C / --add-dir, so it
# stays single-repo — review each affected repo, or use --prompt for a cross-repo pass.

set -uo pipefail

MODEL="gpt-5.6-sol"
EFFORT="xhigh"  # ladder default; escalate to ultra for high-stakes/cross-cutting reviews
TIMEOUT="600"
CD_DIR="$PWD"
MODE=""
ARG=""
SOLO="false"    # --solo: suppress cross-repo sibling context injection

usage() {
  cat >&2 <<'USAGE'
Usage:
  codex-review.sh --base <branch>     Review current branch vs base branch (e.g. dev)
  codex-review.sh --uncommitted       Review staged + unstaged + untracked changes
  codex-review.sh --commit <sha>      Review the changes introduced by one commit
  codex-review.sh --prompt "<text>"   Freeform read-only consult (no git diff)
Options:
  -C <dir>      Repo root / working dir (default: cwd). evolv Ultra is 3 repos —
                point this at the one (or worktree) holding the change.
  -m <model>    Model (default: gpt-5.6-sol)
  -e <effort>   Reasoning effort: low|medium|high|xhigh|ultra (default: xhigh)
  -t <secs>     Timeout in seconds (default: 600)
  --solo        Single-repo only: suppress the cross-repo sibling-context note that
                --prompt mode injects by default (root <-> -be <-> -fe coupling).
USAGE
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)        MODE="base"; ARG="${2:-}"; shift 2;;
    --uncommitted) MODE="uncommitted"; shift;;
    --commit)      MODE="commit"; ARG="${2:-}"; shift 2;;
    --prompt)      MODE="prompt"; ARG="${2:-}"; shift 2;;
    -C)            CD_DIR="${2:-}"; shift 2;;
    -m)            MODEL="${2:-}"; shift 2;;
    -e)            EFFORT="${2:-}"; shift 2;;
    -t)            TIMEOUT="${2:-}"; shift 2;;
    --solo)        SOLO="true"; shift;;
    -h|--help)     usage;;
    *) echo "Unknown arg: $1" >&2; usage;;
  esac
done

[[ -n "$MODE" ]] || usage
command -v codex >/dev/null 2>&1 || { echo "ERROR: codex CLI not found on PATH" >&2; exit 127; }

# Preflight: the gpt-5.6-* models require codex-cli >= 0.144.1 — older CLIs 400 with
# "model requires a newer version of Codex". Fail fast with the upgrade command rather
# than letting every default call return three raw 400s. Skip silently if the version
# can't be parsed (the reactive "Bad model (400) -> drop -m" fallback still applies).
MIN_CLI_FOR_56="0.144.1"
if [[ "$MODEL" == gpt-5.6-* ]]; then
  CLI_VER="$(codex --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  if [[ -n "$CLI_VER" && "$(printf '%s\n%s\n' "$MIN_CLI_FOR_56" "$CLI_VER" | sort -V | head -1)" != "$MIN_CLI_FOR_56" ]]; then
    echo "ERROR: model '$MODEL' requires codex-cli >= $MIN_CLI_FOR_56 (found $CLI_VER)." >&2
    echo "       Upgrade: npm i -g @openai/codex@latest   — or pass an older model, e.g. -m gpt-5.5" >&2
    exit 3
  fi
fi

# --- cross-repo context ------------------------------------------------------------
# The evolv Ultra platform is three coupled git repos. read-only codex already has
# full-disk read, so it CAN read every repo from any one; this block just resolves the
# sibling checkouts and builds a note telling codex they exist + how they couple, so a
# --prompt consult isn't blind to the others. Resolution is worktree-safe: it tries the
# parent of the current repo first, then the two canonical checkout roots.
XCTX=""
if [[ "$SOLO" != "true" ]]; then
  _top="$(cd "$CD_DIR" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)"
  _parent="$(dirname "${_top:-$CD_DIR}")"
  _canon() {  # echo first existing checkout for repo dir-name $1 ($1/.git = repo or worktree)
    local n="$1" p
    for p in "$_parent/$n" "/Volumes/_repos/$n" "$HOME/repos/$n"; do
      [[ -n "$p" && -e "$p/.git" ]] && { printf '%s' "$p"; return 0; }
    done
    return 1
  }
  _be="$(_canon evolv-ultra-be || true)"
  _fe="$(_canon evolv-ultra-fe || true)"
  _root="$(_canon evolv-ultra || true)"
  if [[ -n "$_be$_fe$_root" ]]; then
    XCTX="Cross-repo context — evolv Ultra is three coupled git repos and you have read access to all of them, so consult them as needed (do not review a sibling's diff unless asked):"
    [[ -n "$_be"   ]] && XCTX+=$'\n'"- Backend (FastAPI / Python):        $_be"
    [[ -n "$_fe"   ]] && XCTX+=$'\n'"- Frontend (Next.js / TypeScript):   $_fe"
    [[ -n "$_root" ]] && XCTX+=$'\n'"- Root (docker-compose, specs, ADRs): $_root"
    XCTX+=$'\n'"Coupling that breaks across repos: the frontend vendors the backend's openapi.json and tool-catalog.json (CI freshness gates enforce sync), so a backend response-schema / route / tool-definition change can break frontend types or callers. When a finding has cross-repo surface, open the sibling repo to confirm and flag the contract break explicitly (e.g. a changed BE response the FE type or caller no longer matches, a renamed/removed route the FE still calls)."
  fi
fi

# Portable timeout: GNU `timeout`, BSD/brew `gtimeout`, else a perl alarm() shim.
# macOS ships none of the first two but always has perl. The perl shim sets an alarm
# then exec()s the command (the timer survives exec); on fire, default SIGALRM kills
# it with exit 142. So treat BOTH 124 (GNU) and 142 (perl) as "timed out" below.
if   command -v timeout  >/dev/null 2>&1; then run() { timeout  "$TIMEOUT" "$@"; }
elif command -v gtimeout >/dev/null 2>&1; then run() { gtimeout "$TIMEOUT" "$@"; }
elif command -v perl     >/dev/null 2>&1; then run() { perl -e 'my $t=shift; alarm $t; exec @ARGV or die "exec: $!\n"' "$TIMEOUT" "$@"; }
else echo "WARN: no timeout/gtimeout/perl — running codex unbounded" >&2; run() { "$@"; }
fi

OUT="${TMPDIR:-/tmp}/codex-review-$$.md"

case "$MODE" in
  prompt)
    # Freeform consult: -C + -s read-only are exec-only flags (not on `exec review`).
    # Long/code-heavy prompts are fragile via argv — pass `--prompt -` (or empty) and
    # pipe the prompt via heredoc; codex exec reads it from stdin. The cross-repo
    # context note (XCTX) is prepended so the consult can see all three repos.
    PFX=""
    [[ -n "$XCTX" ]] && PFX="$XCTX"$'\n\n--- end cross-repo context; the request follows ---\n\n'
    if [[ "$ARG" == "-" || -z "$ARG" ]]; then
      # prompt arrives on stdin (heredoc); emit XCTX first, then pass stdin through.
      { [[ -n "$PFX" ]] && printf '%s' "$PFX"; cat; } | \
        run codex exec -s read-only -m "$MODEL" \
          -c model_reasoning_effort="$EFFORT" -C "$CD_DIR" --skip-git-repo-check -o "$OUT"
    else
      run codex exec -s read-only -m "$MODEL" \
        -c model_reasoning_effort="$EFFORT" -C "$CD_DIR" --skip-git-repo-check -o "$OUT" "${PFX}${ARG}"
    fi
    rc=$?
    ;;
  base|uncommitted|commit)
    # `exec review` has no -C; cd into the repo first.
    cd "$CD_DIR" || { echo "ERROR: cannot cd to $CD_DIR" >&2; exit 1; }
    case "$MODE" in
      base)        run codex exec review --base "$ARG"  -m "$MODEL" -c model_reasoning_effort="$EFFORT" -o "$OUT";;
      uncommitted) run codex exec review --uncommitted  -m "$MODEL" -c model_reasoning_effort="$EFFORT" -o "$OUT";;
      commit)      run codex exec review --commit "$ARG" -m "$MODEL" -c model_reasoning_effort="$EFFORT" -o "$OUT";;
    esac
    rc=$?
    ;;
esac

if [[ "${rc:-1}" -eq 124 || "${rc:-1}" -eq 142 ]]; then
  echo "ERROR: codex timed out after ${TIMEOUT}s (raise with -t, or run in background)" >&2
fi
# Cross-repo reminder: `exec review` is git-scoped to ONE repo and takes no context,
# so it can't see the sibling repos. If the change has cross-repo surface, also review
# the other affected repo, or run a `--prompt` cross-repo contract pass.
if [[ "$MODE" != "prompt" && "$SOLO" != "true" && -n "$XCTX" ]]; then
  echo "NOTE: '$MODE' was a single-repo review (codex's review harness takes no cross-repo context)." >&2
  echo "      Cross-repo surface (BE schema/route/tool <-> FE consumer)? Review each affected repo," >&2
  echo "      or run '--prompt' for a contract pass — siblings are readable. ('--solo' silences this.)" >&2
fi
echo "CODEX_REVIEW_OUTPUT=$OUT"
exit "${rc:-1}"
