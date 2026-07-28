"""The per-worker permission gate (FR-WS3/FR-WS4, ADR-0005's shape per-worker).

Every worker tool call routes through `can_use_tool`: AskUserQuestion escalates
via the question bridge; everything else passes the tool ceiling, the cwd pin,
and optional repo guard hooks — default deny. The sidecar's stdio-tee relay is
replaced, not ported.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from .events import EventLog
from .registry import Registry

# Tool-input keys that carry filesystem paths (cwd pin scope). Bash is governed
# by the ceiling's command matchers, not path inspection.
_PATH_KEYS = ("file_path", "path", "notebook_path", "directory")

# Escalation + skills must exist for every worker: /handover write|restore rides
# the Skill tool (G10), questions ride AskUserQuestion.
_ALWAYS_BASE_TOOLS = ("AskUserQuestion", "Skill")

DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
    "Skill",
    "TodoWrite",
    "AskUserQuestion",
]


@dataclass
class WorkerPolicy:
    """Spawn-time policy, persisted as workers.policy JSON."""

    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    allow_env: list[str] = field(default_factory=list)
    guard_hooks: dict[str, str] = field(default_factory=dict)  # tool -> .claude/hooks script
    model: str | None = None
    limits: dict[str, Any] = field(default_factory=dict)  # per-worker Limits overrides
    # Per-lane MCP grant (ECA-100): server-name -> SDK McpServerConfig, passed to
    # ClaudeAgentOptions.mcp_servers. Any credential a server needs lives in ITS
    # own env/headers block here, NOT the worker's process env (envbuild's A3
    # scrub stays intact). Empty = no MCP tools for the lane.
    mcp_servers: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: str) -> "WorkerPolicy":
        data = json.loads(raw or "{}")
        return cls(
            allowed_tools=data.get("allowed_tools", list(DEFAULT_ALLOWED_TOOLS)),
            allow_env=data.get("allow_env", []),
            guard_hooks=data.get("guard_hooks", {}),
            model=data.get("model"),
            limits=data.get("limits", {}),
            mcp_servers=data.get("mcp_servers", {}),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "allowed_tools": self.allowed_tools,
                "allow_env": self.allow_env,
                "guard_hooks": self.guard_hooks,
                "model": self.model,
                "limits": self.limits,
                "mcp_servers": self.mcp_servers,
            }
        )

    def base_tools(self) -> list[str]:
        """Base-set restriction for ClaudeAgentOptions.tools: names before '('.

        Tools not listed here DO NOT EXIST for the session (G8) — the ceiling's
        hard floor. Escalation/skill tools are always present.
        """
        names: list[str] = []
        for spec in self.allowed_tools:
            base = spec.split("(", 1)[0].strip()
            # MCP tool existence is governed by the connected mcp_servers, not by
            # the built-in --tools floor; a literal 'mcp__server__*' here would be
            # meaningless (and could confuse the flag). Skip it — the ceiling still
            # gates the calls (ceiling_allows handles the name wildcard).
            if base.startswith("mcp__"):
                continue
            if base and base not in names:
                names.append(base)
        for required in _ALWAYS_BASE_TOOLS:
            if required not in names:
                names.append(required)
        return names

    def ceiling_allows(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """Grant-spec match: bare 'Tool' allows all inputs; 'Bash(prefix*)' is a
        prefix matcher on the command field (Claude Code matcher semantics)."""
        for spec in self.allowed_tools:
            base, _, matcher = spec.partition("(")
            base = base.strip()
            # Tool-NAME wildcard (ECA-100): a spec ending in '*' with no '(...)'
            # matcher grants every tool whose name shares that prefix — the way to
            # allow a whole MCP server, e.g. 'mcp__jira__*' -> mcp__jira__search.
            if not matcher and base.endswith("*"):
                if tool_name.startswith(base[:-1]):
                    return True
                continue
            if base != tool_name:
                continue
            if not matcher:  # bare tool name: all inputs
                return True
            pattern = matcher.rstrip(")").strip()
            command = str(tool_input.get("command", ""))
            if pattern.endswith("*"):
                if command.startswith(pattern[:-1]):
                    return True
            elif command == pattern:
                return True
        return False


def redact_policy(policy: Any) -> dict[str, Any]:
    """Return a copy of a policy mapping safe to hand back over the control surface.

    `mcp_servers` is the one credential-bearing part of a WorkerPolicy: each
    server carries whatever it needs to authenticate INLINE (an http server's
    `headers`, a stdio server's `env` — and nothing stops a token riding in
    `args` or a `url` query string). Rather than redact by key name and miss a
    shape, the whole block collapses to a sorted list of the granted server
    NAMES — which is already what every other surface in this daemon exposes
    (the engine's options snapshot, its MCP diagnostics, and failure capsules
    all use `sorted(policy.mcp_servers.keys())`).

    Nothing needs the values back: the caller supplied them moments earlier, and
    echoing them turns every `workers spawn` into a credential disclosure in the
    caller's transcript — which is persisted, shipped to a model provider, and
    read by later sessions (ECA-133). Idempotent: re-redacting an already
    redacted policy is a no-op.
    """
    out = dict(policy or {})
    servers = out.get("mcp_servers")
    if isinstance(servers, dict):
        out["mcp_servers"] = sorted(servers.keys())
    elif isinstance(servers, list):  # already redacted
        out["mcp_servers"] = sorted(servers)
    elif servers is not None:
        # Unrecognized shape — never pass an unknown value through; it could be
        # anything, including a credential.
        out["mcp_servers"] = []
    return out


def redact_worker_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a `workers` registry row whose policy carries no secrets.

    The row's `policy` column is a JSON string (SQLite), and the redacted copy
    keeps that shape so response consumers see no structural change — only the
    `mcp_servers` block differs. Every control-surface response that includes a
    worker row MUST go through this (see `server.ControlServer._dispatch`).

    The dict branch is defensive, not a live path: today every row comes from the
    TEXT column, so `policy` is always a `str`. Anything that is neither a JSON
    object nor absent — a bare list, a scalar, unparseable text — is withheld
    rather than guessed at: we cannot prove it holds no secret, so it does not
    leave the daemon.
    """
    out = dict(row)
    raw = out.get("policy")
    if raw is None:
        return out
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            out["policy"] = None
            return out
        out["policy"] = json.dumps(redact_policy(parsed)) if isinstance(parsed, dict) else None
    elif isinstance(raw, dict):
        out["policy"] = redact_policy(raw)
    else:
        out["policy"] = None
    return out


class QuestionBridge:
    """Parks AskUserQuestion escalations; answers arrive over the control surface.

    The asking turn's SDK stream stays open inside can_use_tool until the answer
    future resolves or the question timeout fires (FR-WS4: never blocks forever,
    never wedges another worker — the wait is per-worker, inside its own turn).
    """

    def __init__(self, registry: Registry, events: EventLog) -> None:
        self._registry = registry
        self._events = events
        self._waiters: dict[int, asyncio.Future[str]] = {}

    async def ask(
        self, worker: str, turn_id: int, questions_payload: Any, timeout_s: float
    ) -> str | None:
        """Returns the answer text, or None on timeout (caller ends the turn)."""
        qid = await self._registry.park_question(turn_id, worker, questions_payload)
        await self._registry.set_worker_status(worker, "needs_input")
        self._events.emit(worker, "question_parked", question_id=qid, turn_id=turn_id)
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[qid] = fut
        try:
            answer = await asyncio.wait_for(fut, timeout=timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            await self._registry.resolve_question(qid, "timed_out", None)
            self._events.emit(worker, "question_timeout", question_id=qid, turn_id=turn_id)
            return None
        finally:
            self._waiters.pop(qid, None)
            await self._registry.set_worker_status(worker, "running")
        self._events.emit(worker, "question_answered", question_id=qid, turn_id=turn_id)
        return answer

    async def answer(self, question_id: int, text: str) -> bool:
        """CAS-resolve the question and wake the parked turn. False if not pending."""
        won = await self._registry.resolve_question(question_id, "answered", text)
        if won:
            fut = self._waiters.get(question_id)
            if fut is not None and not fut.done():
                fut.set_result(text)
        return won


def make_question_hook(
    *,
    worker: str,
    turn_id: int,
    bridge: QuestionBridge,
    question_timeout_s: float,
):
    """PreToolUse hook interception for AskUserQuestion.

    On CLI 2.1.165 AskUserQuestion is a UI tool, not a permission-gated one:
    it never reaches can_use_tool and errors headless ("stream closed"). Hooks
    fire for every tool use, so the bridge lives here; the hook's deny reason
    carries the answer back (the eck-dev contract, relocated).
    """

    async def on_ask_user_question(hook_input: Any, tool_use_id: str | None, context: Any):
        tool_input = (hook_input or {}).get("tool_input", {}) or {}
        payload = tool_input.get("questions", tool_input)
        answer = await bridge.ask(worker, turn_id, payload, question_timeout_s)
        if answer is None:
            return {
                "decision": "block",
                "reason": "No answer arrived before the question timeout.",
                "continue_": False,
                "stopReason": "question timed out unanswered",
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"The user responded: {answer}",
            }
        }

    return on_ask_user_question


async def _run_guard_hook(
    repo_root: Path, script: str, tool_name: str, tool_input: dict[str, Any]
) -> tuple[str, str]:
    """Run a repo .claude/hooks guard with the hook JSON contract on stdin.

    Returns (decision, reason): decision in allow/deny/ask/error/none.
    """
    hook_path = repo_root / ".claude" / "hooks" / script
    if not hook_path.exists():
        return "none", f"guard hook missing: {script}"
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    proc = await asyncio.create_subprocess_exec(
        "bash",
        str(hook_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=repo_root,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(payload.encode()), timeout=10)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        return "error", f"guard hook timed out: {script}"
    if not stdout.strip():
        return ("allow", "") if proc.returncode == 0 else ("error", "guard hook failed")
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError:
        return "error", f"guard hook emitted non-JSON: {script}"
    specific = out.get("hookSpecificOutput", {})
    return specific.get("permissionDecision", "allow"), specific.get("message", "")


def _path_escapes(repo_root: Path, tool_input: dict[str, Any]) -> str | None:
    """Realpath-check every path-carrying input against the worker's repo root.

    Returns the offending path, or None if all paths are contained.
    """
    root = repo_root.resolve()
    for key in _PATH_KEYS:
        raw = tool_input.get(key)
        if not raw or not isinstance(raw, str):
            continue
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = root / p
        resolved = p.resolve()  # follows symlinks; escape via symlink is caught
        try:
            resolved.relative_to(root)
        except ValueError:
            return raw
    return None


def make_gate(
    *,
    worker: str,
    repo_root: Path,
    policy: WorkerPolicy,
    bridge: QuestionBridge,
    events: EventLog,
    turn_id: int,
    question_timeout_s: float,
) -> Callable[[str, dict[str, Any], Any], Awaitable[Any]]:
    """Build the can_use_tool callback for ONE turn of ONE worker."""

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any):
        # 1. Escalation channel: park, wait, deny-with-answer (the eck-dev bridge,
        #    current questions[] schema — A-WS2).
        if tool_name == "AskUserQuestion":
            payload = tool_input.get("questions", tool_input)
            answer = await bridge.ask(worker, turn_id, payload, question_timeout_s)
            if answer is None:
                return PermissionResultDeny(
                    message="No answer arrived before the question timeout; stop this turn.",
                    interrupt=True,
                )
            return PermissionResultDeny(message=f"The user responded: {answer}")

        # 2. Tool ceiling (the base set already restricts existence; this enforces
        #    grant matchers like Bash(uv run*) on top).
        if not policy.ceiling_allows(tool_name, tool_input):
            reason = f"tool {tool_name!r} is outside this worker's ceiling"
            events.emit(worker, "tool_denied", turn_id=turn_id, tool=tool_name, reason=reason)
            return PermissionResultDeny(message=f"Denied by worker policy: {reason}")

        # 3. cwd pin: path-carrying inputs must stay under the worker's repo root.
        offending = _path_escapes(repo_root, tool_input)
        if offending is not None:
            reason = f"path {offending!r} escapes the worker root {str(repo_root)!r}"
            events.emit(worker, "tool_denied", turn_id=turn_id, tool=tool_name, reason=reason)
            return PermissionResultDeny(message=f"Denied by worker policy: {reason}")

        # 4. Optional repo guard hooks (eck-dev hook-contract runner). 'ask' has no
        #    human to ask here — MVP treats it as deny with reason (escalation of
        #    denied calls is deferred by design).
        script = policy.guard_hooks.get(tool_name)
        if script:
            decision, reason = await _run_guard_hook(repo_root, script, tool_name, tool_input)
            if decision in ("deny", "ask", "error"):
                events.emit(
                    worker, "tool_denied", turn_id=turn_id, tool=tool_name,
                    reason=f"guard:{decision}:{reason}",
                )
                return PermissionResultDeny(
                    message=f"Denied by repo guard hook ({decision}): {reason or script}"
                )

        return PermissionResultAllow()

    return can_use_tool
