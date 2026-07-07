#!/usr/bin/env bash
# ECA-64 end-to-end smoke (AC#5). Plays the spawner's relay role so it runs
# BEFORE ECA-65 exists: build -> stand up the internal no-egress net + proxy ->
# 0400 secret bind-mounts -> full hardened `docker run` -> clone a repo -> one
# SDK turn -> assert result.json reaches the driver; plus limits / egress / layer
# proofs.
#
# Model leg (Q4): DEFAULT is the cred-free replay leg (SANDBOX_RUNNER_REPLAY), so
# the clone/limits/egress/layer legs run live with NO Bedrock bearer. Provide a
# real bearer to run the model leg live:
#     ECA_BEDROCK_SECRET=/path/to/bearer ECA_ANTHROPIC_MODEL=us.anthropic... ./smoke.sh
#
# Requires: docker (Desktop VM is fine). Exit 0 = all legs passed.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_DIR="$(cd "$HERE/.." && pwd)"
PROXY_DIR="$RUNNER_DIR/egress-proxy"
SECCOMP="$RUNNER_DIR/seccomp/eca-seccomp.json"

AGENT_IMAGE="${ECA_AGENT_IMAGE:-eca/agent-sandbox:dev}"
NET_INTERNAL="${ECA_NET_INTERNAL:-eca-egress-internal}"
PROXY_NAME="${ECA_PROXY_NAME:-eca-egress-proxy}"
PROXY_URL="http://$PROXY_NAME:8888"

# A tiny public repo for the clone leg (no token needed for a public read; the
# credential helper is only consulted on a 401, which public clone never returns).
CLONE_URL="${ECA_CLONE_URL:-https://github.com/octocat/Hello-World.git}"
CLONE_REF="${ECA_CLONE_REF:-master}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/eca-smoke.XXXXXX")"
PASS=0 FAIL=0

log()  { printf '\n\033[1m[smoke] %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

cleanup() {
  log "cleanup"
  docker rm -f eca-agent-smoke >/dev/null 2>&1 || true
  ECA_NET_INTERNAL="$NET_INTERNAL" ECA_PROXY_NAME="$PROXY_NAME" \
    "$PROXY_DIR/net-down.sh" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# ---- fixtures: 0400 secrets (AC#3) ------------------------------------------
# A DUMMY gh token satisfies the runner's token_present() gate; it is never sent
# for a public clone. Replace with a real fine-grained token for a private repo.
GH_TOKEN="$WORK/gh_token"; printf 'x-access-token-dummy' > "$GH_TOKEN"; chmod 0400 "$GH_TOKEN"

BEDROCK_ARGS=()
REPLAY_DEFAULT=1
if [ -n "${ECA_BEDROCK_SECRET:-}" ] && [ -f "${ECA_BEDROCK_SECRET}" ]; then
  log "live model leg: bearer at $ECA_BEDROCK_SECRET"
  BEDROCK_ARGS=(-v "$ECA_BEDROCK_SECRET:/run/secrets/bedrock:ro")
  REPLAY_DEFAULT=0
fi

# ---- hardened run helper -----------------------------------------------------
# run_agent <job-name> <request-json-file> [extra -e/-v args...]
# Echoes nothing; writes result.json into $WORK/<job-name>/.
run_agent() {
  local name="$1" req="$2"; shift 2
  local jd="$WORK/$name"; mkdir -p "$jd"; cp "$req" "$jd/request.json"; chmod -R 777 "$jd"
  docker run --rm --name "eca-agent-smoke" \
    --network "$NET_INTERNAL" \
    -e HTTPS_PROXY="$PROXY_URL" -e https_proxy="$PROXY_URL" \
    -e HTTP_PROXY="$PROXY_URL"  -e http_proxy="$PROXY_URL" \
    -e NO_PROXY="localhost,127.0.0.1" -e no_proxy="localhost,127.0.0.1" \
    --user 1000:1000 \
    --cap-drop=ALL \
    --security-opt no-new-privileges \
    --security-opt "seccomp=$SECCOMP" \
    --read-only \
    --tmpfs /work:rw,size=2g,mode=1777 \
    --tmpfs /tmp:rw,size=256m,mode=1777 \
    --pids-limit 512 --memory 4g --cpus 2 \
    -e HOME=/work \
    -v "$jd:/job" \
    -v "$GH_TOKEN:/run/secrets/gh_token:ro" \
    ${BEDROCK_ARGS[@]+"${BEDROCK_ARGS[@]}"} \
    "$@" \
    "$AGENT_IMAGE" --job-dir /job >"$jd/stdout.log" 2>"$jd/stderr.log"
}

result_state() {  # result_state <job-name>
  python3 -c "import json,sys;print(json.load(open('$WORK/$1/result.json'))['state'])" 2>/dev/null \
    || echo "NO_RESULT"
}

# =============================================================================
log "1. build images"
docker build -q -t "$AGENT_IMAGE" "$RUNNER_DIR" >/dev/null && ok "agent image built" || { bad "agent image build"; exit 1; }
ECA_PROXY_BUILD=1 ECA_NET_INTERNAL="$NET_INTERNAL" ECA_PROXY_NAME="$PROXY_NAME" \
  "$PROXY_DIR/net-up.sh" >/dev/null && ok "proxy + internal net up" || { bad "net-up"; exit 1; }

# =============================================================================
log "2. end-to-end: clone -> one turn -> result.json reaches the driver"
cat > "$WORK/e2e.json" <<JSON
{ "job_id": "smoke-e2e",
  "prompt": "Summarize the README in three bullets.",
  "repo": { "url": "$CLONE_URL", "ref": "$CLONE_REF", "clone": true, "depth": 1,
            "worktree": "/job/repo" },
  "limits": { "wall_clock_s": 120, "max_turns": 1, "max_budget_usd": 1.0 } }
JSON
E2E_ENV=()
[ "$REPLAY_DEFAULT" = "1" ] && E2E_ENV=(-e SANDBOX_RUNNER_REPLAY=1)
[ -n "${ECA_ANTHROPIC_MODEL:-}" ] && E2E_ENV+=(-e "ANTHROPIC_MODEL=$ECA_ANTHROPIC_MODEL")
run_agent e2e "$WORK/e2e.json" ${E2E_ENV[@]+"${E2E_ENV[@]}"}
st="$(result_state e2e)"
[ "$st" = "completed" ] && ok "e2e result state=completed" || bad "e2e result state=$st (expected completed)"
# Clone lands in the host-visible /job mount (repo.worktree=/job/repo) so the
# driver can prove the clone+egress succeeded from outside the container.
[ -f "$WORK/e2e/repo/README" ] && ok "repo cloned via proxy egress (README present in /job mount)" \
  || bad "clone leg: README not found (clone/egress failure?)"
grep -q '"total_cost_usd"' "$WORK/e2e/result.json" 2>/dev/null \
  && ok "result carries total_cost_usd/usage" || bad "result missing cost fields"

# =============================================================================
log "3. limits proofs (each limit demonstrably bites)"
# turn-limit + budget: replay a canned terminal subtype (proves limit->state
# plumbing end-to-end through the container; native SDK enforcement is covered by
# the unit tests + the live leg). wall-clock is enforced live by the runner.
_limit_job() {  # _limit_job <name> <replay-json> <wall_clock_s> <expect-state>
  # NOTE(bash 3.2): keep jd on its own `local` line — referencing $name in the
  # same `local` as its assignment expands the (unset) outer name under set -u.
  local name="$1" replay="$2" wall="$3" expect="$4"
  local jd="$WORK/$name"
  mkdir -p "$jd"; printf '%s' "$replay" > "$jd/replay.json"
  cat > "$jd/req.json" <<JSON
{ "job_id": "$name", "prompt": "noop",
  "limits": { "wall_clock_s": $wall, "max_turns": 1, "max_budget_usd": 0.01 } }
JSON
  run_agent "$name" "$jd/req.json" -e SANDBOX_RUNNER_REPLAY=/job/replay.json
  local st; st="$(result_state "$name")"
  [ "$st" = "$expect" ] && ok "$name -> state=$expect" || bad "$name -> state=$st (expected $expect)"
}
_limit_job turnlimit '{"messages":[{"type":"result","subtype":"error_max_turns","is_error":true}]}' 120 turn_limit
_limit_job budget    '{"messages":[{"type":"result","subtype":"error_max_budget","is_error":true}]}' 120 budget_exceeded
_limit_job walltime  '{"pre_sleep_s":8,"messages":[{"type":"result","subtype":"success"}]}'          1   timeout

# =============================================================================
log "4. egress proof (default-deny allowlist)"
# Probe from the agent image itself (has python) on the internal net, via the proxy.
probe() {  # probe <url> ; prints REACHED|BLOCKED
  docker run --rm --network "$NET_INTERNAL" \
    -e HTTPS_PROXY="$PROXY_URL" -e https_proxy="$PROXY_URL" \
    --cap-drop=ALL --security-opt no-new-privileges \
    --entrypoint python "$AGENT_IMAGE" -c "
import urllib.request, urllib.error, sys
try:
    urllib.request.urlopen('$1', timeout=25); print('REACHED')
except urllib.error.HTTPError: print('REACHED')   # origin answered => tunnel allowed
except Exception: print('BLOCKED')
" 2>/dev/null
}
g="$(probe https://github.com)"
[ "$g" = "REACHED" ] && ok "github.com REACHED (allowlisted)" || bad "github.com $g (expected REACHED)"
b="$(probe https://bedrock-runtime.us-east-1.amazonaws.com)"
[ "$b" = "REACHED" ] && ok "bedrock-runtime REACHED (allowlisted)" || bad "bedrock $b (expected REACHED)"
e="$(probe https://example.com)"
[ "$e" = "BLOCKED" ] && ok "example.com BLOCKED (default-deny)" || bad "example.com $e (expected BLOCKED — LEAK)"

# =============================================================================
log "5. layer proof (AC#3: no secret in image history / env / fs)"
hist="$(docker history --no-trunc "$AGENT_IMAGE" 2>/dev/null)"
env_json="$(docker inspect -f '{{json .Config.Env}}' "$AGENT_IMAGE" 2>/dev/null)"
if printf '%s' "$hist$env_json" | grep -Eiq 'AWS_BEARER_TOKEN_BEDROCK=|x-access-token-dummy|gh_token=|password='; then
  bad "secret material found in image history/env"
else
  ok "no secret in docker history / Config.Env"
fi
if printf '%s' "$env_json" | grep -q 'CLAUDE_CODE_USE_BEDROCK=1'; then
  ok "only the non-secret env belt is baked in"
else
  bad "expected non-secret belt missing from Config.Env"
fi
# filesystem: the token file must NOT exist in the image (only appears at runtime as a mount)
if docker run --rm --entrypoint sh "$AGENT_IMAGE" -c 'test ! -e /run/secrets/gh_token && test ! -e /run/secrets/bedrock' 2>/dev/null; then
  ok "image fs carries no baked secret files"
else
  bad "image fs contains a secret file"
fi

# =============================================================================
log "summary: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
