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
from .names import HOOK_SCRIPT_RE, is_safe_hook_script
from .registry import Registry

# Tool-input keys that carry filesystem paths (cwd pin scope). Bash is governed
# by the ceiling's command matchers (`ceiling_denial`), not path inspection —
# nothing here reads a path out of a shell command.
_PATH_KEYS = ("file_path", "path", "notebook_path", "directory")

# Escalation + skills must exist for every worker: /handover write|restore rides
# the Skill tool (G10), questions ride AskUserQuestion.
_ALWAYS_BASE_TOOLS = ("AskUserQuestion", "Skill")

# Shell operators that separate one command from the next, longest-first so '&&'
# is consumed before '&' and '|&' before '|'. A command's segments are matched
# against the ceiling INDEPENDENTLY (ECA-144) — this is Claude Code's own rule
# for compound commands, and the list is its documented set plus newline.
_COMMAND_SEPARATORS = ("&&", "||", "|&", ";", "|", "&", "\n")

# Redirection operators. A redirect TARGET is not a command, so "every segment
# must match a grant" says nothing about it — see `_split_command_segments` for
# why a matcher-granted command carrying one is refused rather than ignored.
_REDIRECTIONS = (">>", "<<<", "<<", ">", "<")

# Shell syntax this parser does NOT model, and therefore REFUSES outside single
# quotes. Every one of these was a working bypass in review, because each changes
# where a shell thinks a quote or a command ends:
#
#   `$'`  ANSI-C quoting. Inside it `\'` is an ESCAPED quote, so a shell's string
#         ends later than a POSIX-single-quote scan thinks. One quote out of
#         phase is enough to hide a whole `$(...)` from the splitter:
#         `echo $'\''$(id -un)\'` ran `id` under a `Bash(echo *)` grant.
#   `$"`  locale translation, same quoting subtleties.
#   `#`   a comment — and a shell does NOT apply line continuation inside one, so
#         `echo hi #\<newline>id -un` is two commands to a shell and looked like
#         one to the splitter.
#   `\`+newline  line continuation: a shell splices the lines BEFORE tokenising,
#         even inside double quotes, so `echo "$\<newline>(id -un)"` is really
#         `echo "$(id -un)"`. Recognising the substitution after splicing means
#         re-implementing the splice; refusing is one line and cannot be subtly
#         wrong.
#
# Modelling each of them is possible and is how the first attempt at this fix was
# written. The lesson from those three bypasses is the opposite one: a hand-rolled
# shell tokenizer used as a security boundary should REFUSE what it does not model
# rather than approximate it, exactly as it already refuses redirection. Quote the
# construct (single quotes are fully modelled) or grant the lane a bare `Bash`.
_UNMODELLED_SYNTAX = ("$'", '$"', "#")


class UnparseableCommand(Exception):
    """A Bash command that cannot be split into segments we can vouch for.

    Raised — and turned into a REFUSAL by the only caller — rather than returning
    a best guess. See `_split_command_segments`.
    """


# A substitution's inner command is judged on its own, and the text it occupied in
# the enclosing command is replaced by this rather than deleted: `ls $(git rev-parse
# --show-toplevel)` must still look like `ls <something>` to an `ls *` grant, or a
# composition of two individually-granted commands would be refused for no reason.
_SUBSTITUTION_PLACEHOLDER = "$()"

# Substitutions and groups nest, and this parser recurses per level on
# model-supplied text. Bounded so a pathological command is REFUSED (the
# documented contract) instead of raising RecursionError — which on the
# `can_use_tool` path escaped as an exception and wrote no audit event.
_MAX_NESTING = 32


def _split_command_segments(command: str, depth: int = 0) -> list[str]:
    """Split a shell command into the individual commands it would run.

    Why this exists: `ceiling_allows` used to test the whole command string with
    `startswith`, so a `Bash(echo*)` grant allowed `echo hi && cat /etc/passwd`
    (ECA-144). Every separated command has to face the ceiling on its own.

    What one pass produces: the top-level segments split on `_COMMAND_SEPARATORS`,
    plus the inner command of every substitution (`$(...)`, backticks, `<(...)`,
    `>(...)`) and of every `(...)` subshell, hoisted out as segments of their own
    and replaced by `_SUBSTITUTION_PLACEHOLDER` in the text they were embedded in.
    Hoisting matters in both directions: the inner command must be judged (it
    executes), and the outer segment must not be judged with the substitution still
    glued to it (a grant for `echo` should not have to anticipate `echo $(...)`).

    Quoting: a single-quoted run is POSIX — no escapes inside it, so it ends at the
    next `'` — and a backslash escape outside quotes makes one character literal.
    So `echo 'a && b'` is ONE segment. Double quotes suppress operators but NOT
    substitution, because a shell still executes `$(...)` inside them; that
    asymmetry is why this parses rather than pattern-matches. Anything whose
    quoting rules differ from those two is refused, not approximated — see
    `_UNMODELLED_SYNTAX`, which exists because guessing at them was three separate
    arbitrary-command bypasses.

    **This function fails closed** (ECA-144 AC#2). It raises `UnparseableCommand`
    for input it cannot account for, and the caller REFUSES the call rather than
    allowing it:

    - unbalanced quotes, or an unterminated substitution/group — the tail cannot
      be attributed to any segment, so a grant cannot be said to cover it;
    - any construct in `_UNMODELLED_SYNTAX`, plus a line continuation;
    - nesting deeper than `_MAX_NESTING`;
    - a redirection operator (`>`, `>>`, `<`, `<<`, `<<<`) anywhere outside
      quotes. A redirect is not a command, so requiring it to "match a grant" is
      meaningless, and silently ignoring it would let a lane granted
      `Bash(echo *)` run `echo x > ~/.zshrc` — the same class of defect as this
      task. Claude Code's docs do not say either way what it does with a
      redirection inside a matched command, so treat this as OUR fail-closed
      choice rather than a documented divergence. Grant bare `Bash` to a lane
      that needs to redirect.

    Deliberately NOT modelled, because none of it can widen what runs: variable
    expansion (`$FOO` and `${FOO}` are left in the segment as written — an
    expansion that produces an operator is expanded after word-splitting by the
    shell and cannot introduce a new command; a `$(...)` INSIDE a `${...}` is
    still hoisted, because the scan reaches it), aliases, and `env`-style
    prefixes. A pattern is matched against the segment's literal text, so an
    unexpanded `$FOO` simply fails to match a narrow grant.

    Two constructs are over-refused rather than modelled, which is the safe
    direction: a `{ ...; }` brace group leaves its braces in the segments, and
    `$((...))` arithmetic contributes its expression as a segment. Neither matches
    a narrow grant, so both deny. A lane that needs either wants bare `Bash`.
    """
    if depth > _MAX_NESTING:
        raise UnparseableCommand(f"nested deeper than {_MAX_NESTING} levels")
    segments: list[str] = []
    current: list[str] = []
    i = 0
    n = len(command)

    def flush() -> None:
        text = "".join(current).strip()
        if text:
            segments.append(text)
        current.clear()

    while i < n:
        ch = command[i]

        unmodelled = next(
            (s for s in _UNMODELLED_SYNTAX if command.startswith(s, i)), None
        )
        if unmodelled is not None:
            raise UnparseableCommand(
                f"{unmodelled!r} is shell syntax this matcher does not model"
            )

        if ch == "\\":  # escape: the next character is literal, operator or not
            if i + 1 >= n:
                # A trailing backslash is a line continuation with nothing to
                # continue. Refuse rather than guess what the shell would join.
                raise UnparseableCommand("command ends in a dangling backslash")
            if command[i + 1] == "\n":
                raise UnparseableCommand("line continuation is not modelled")
            current.append(command[i : i + 2])
            i += 2
            continue

        if ch == "'":
            # POSIX single quotes: no escapes inside, so the run ends at the very
            # next quote. Do NOT "fix" this to skip escaped quotes — that is bash's
            # `$'...'` rule, not this one, and applying it here desynchronises the
            # scan from the shell (which is why `$'` is refused outright above).
            end = command.find("'", i + 1)
            if end < 0:
                raise UnparseableCommand("unbalanced single quote")
            current.append(command[i : end + 1])
            i = end + 1
            continue

        if ch == '"':
            # Double quotes suppress operators but not substitution, so the body
            # is walked rather than skipped.
            body, i = _scan_double_quoted(command, i, segments, depth)
            current.append(body)
            continue

        if ch == "`":
            end = _find_unescaped(command, "`", i + 1)
            if end < 0:
                raise UnparseableCommand("unbalanced backtick substitution")
            segments.extend(_split_command_segments(command[i + 1 : end], depth + 1))
            current.append(_SUBSTITUTION_PLACEHOLDER)
            i = end + 1
            continue

        if command.startswith("$(", i):
            end = _match_paren(command, i + 1)
            segments.extend(_split_command_segments(command[i + 2 : end], depth + 1))
            current.append(_SUBSTITUTION_PLACEHOLDER)
            i = end + 1
            continue

        # Process substitution: <(cmd) / >(cmd). Checked BEFORE the redirection
        # scan below, since both start with a redirection character.
        if command.startswith("<(", i) or command.startswith(">(", i):
            end = _match_paren(command, i + 1)
            segments.extend(_split_command_segments(command[i + 2 : end], depth + 1))
            current.append(_SUBSTITUTION_PLACEHOLDER)
            i = end + 1
            continue

        if ch == "(":  # subshell group
            end = _match_paren(command, i)
            segments.extend(_split_command_segments(command[i + 1 : end], depth + 1))
            i = end + 1
            continue

        if ch == ")":
            # Unmatched: the matching '(' would have consumed it.
            raise UnparseableCommand("unbalanced parenthesis")

        redirect = next((op for op in _REDIRECTIONS if command.startswith(op, i)), None)
        if redirect is not None:
            raise UnparseableCommand(f"redirection {redirect!r} is not a command")

        separator = next(
            (op for op in _COMMAND_SEPARATORS if command.startswith(op, i)), None
        )
        if separator is not None:
            flush()
            i += len(separator)
            continue

        current.append(ch)
        i += 1

    flush()
    return segments


def _scan_double_quoted(
    command: str, start: int, segments: list[str], depth: int
) -> tuple[str, int]:
    """Consume a double-quoted run, hoisting any substitution inside it.

    Returns the quoted text with each substitution replaced by
    `_SUBSTITUTION_PLACEHOLDER` (its inner commands appended to `segments`) and the
    index just past the closing quote. A shell executes `$(...)` and backticks
    inside double quotes, so `echo "$(cat x)"` must contribute `cat x` as its own
    segment — this is the case a naive "skip to the closing quote" scan gets wrong.

    A line continuation is refused HERE too, not just at the top level: a shell
    splices `\\<newline>` away before tokenising even inside double quotes, so
    `"$\\<newline>(cmd)"` really is `"$(cmd)"`. Recognising that would mean
    splicing first; refusing cannot be subtly wrong, and getting this wrong was a
    working arbitrary-command bypass (see `_UNMODELLED_SYNTAX`).
    """
    out: list[str] = ['"']
    i = start + 1
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "\\":
            if i + 1 >= n:
                raise UnparseableCommand("command ends in a dangling backslash")
            if command[i + 1] == "\n":
                raise UnparseableCommand("line continuation is not modelled")
            out.append(command[i : i + 2])
            i += 2
            continue
        if ch == '"':
            out.append('"')
            return "".join(out), i + 1
        if command.startswith("$(", i):
            end = _match_paren(command, i + 1)
            segments.extend(_split_command_segments(command[i + 2 : end], depth + 1))
            out.append(_SUBSTITUTION_PLACEHOLDER)
            i = end + 1
            continue
        if ch == "`":
            end = _find_unescaped(command, "`", i + 1)
            if end < 0:
                raise UnparseableCommand("unbalanced backtick substitution")
            segments.extend(_split_command_segments(command[i + 1 : end], depth + 1))
            out.append(_SUBSTITUTION_PLACEHOLDER)
            i = end + 1
            continue
        out.append(ch)
        i += 1
    raise UnparseableCommand("unbalanced double quote")


def _find_unescaped(text: str, char: str, start: int) -> int:
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == char:
            return i
        i += 1
    return -1


def _match_paren(text: str, open_index: int) -> int:
    """Index of the ')' closing the '(' at `open_index`, honouring nesting/quotes."""
    depth = 0
    i = open_index
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise UnparseableCommand("unbalanced single quote")
            i = end + 1
            continue
        if ch == '"':
            end = _find_unescaped(text, '"', i + 1)
            if end < 0:
                raise UnparseableCommand("unbalanced double quote")
            i = end + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise UnparseableCommand("unterminated substitution or group")


def _normalize_matcher(pattern: str) -> str:
    """Apply Claude Code's `:*` rule: a TRAILING `:*` is a trailing wildcard.

    CC's docs: *"The `:*` suffix is an equivalent way to write a trailing
    wildcard"*, and it *"is only recognized at the end of a pattern — in a pattern
    like `Bash(git:* push)` the colon is treated as a literal character"*. So
    `Bash(ls:*)` means `Bash(ls *)`, word boundary included, while the colon in
    `Bash(npm run test:*)`'s middle would be literal.

    This is not a nicety. `cmd:*` is the form CC's own permission dialog writes,
    so it is what an operator copying a grant out of `settings.json` will paste —
    and treating that colon as literal INVERTS the grant (it would admit
    `npm run test:unit` and refuse `npm run test -q`). Getting it backwards was
    caught in review, having been documented in four places as intended.
    """
    if pattern.endswith(":*"):
        return pattern[: -len(":*")] + " *"
    return pattern


def _glob_matches(pattern: str, text: str) -> bool:
    """Anchored `*`-glob match — Claude Code's matcher semantics, whole string.

    `*` matches any run of characters INCLUDING spaces and newlines, at any
    position; every other character is literal. Whether a trailing `*` enforces a
    word boundary therefore falls out of the pattern the author wrote, exactly as
    it does in Claude Code: `Bash(ls *)` requires the space and so does not match
    `lsof`, while `Bash(ls*)` does.

    Iterative with a single backtrack point, NOT a compiled regex. An earlier draft
    translated each `*` to `.*` and called `fullmatch`, which backtracks
    catastrophically: an operator pattern with eight wildcards against a 60-char
    non-matching command took >5s, inside the PreToolUse hook, on every tool call.
    This is O(len(pattern) x len(text)) worst case with no recursion and no
    pathological blowup.
    """
    p = t = 0
    star = -1
    resume = 0
    while t < len(text):
        if p < len(pattern) and pattern[p] == "*":
            star = p
            p += 1
            resume = t
        elif p < len(pattern) and pattern[p] == text[t]:
            p += 1
            t += 1
        elif star >= 0:  # last '*' absorbs one more character
            p = star + 1
            resume += 1
            t = resume
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


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
    # tool -> a script name inside the repo's `.claude/hooks/`. The VALUE is a plain
    # filename component, not a path: it is joined under that directory and executed, so
    # anything else is refused at derivation (ECA-140 — see `_run_guard_hook`). Typed
    # `dict[str, str]` because that is the contract; the runtime does not enforce it,
    # since control-socket JSON reaches this field uncoerced.
    guard_hooks: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    limits: dict[str, Any] = field(default_factory=dict)  # per-worker Limits overrides
    # Per-lane MCP grant (ECA-100): server-name -> SDK McpServerConfig, passed to
    # ClaudeAgentOptions.mcp_servers. Any credential a server needs lives in ITS
    # own env/headers block here, NOT the worker's process env (envbuild's A3
    # scrub stays intact). Empty = no MCP tools for the lane.
    #
    # ECA-135 correction — what that does NOT mean. "Not in the worker's process
    # env" was read as "the worker's own process never handles these credentials".
    # That is false, and was proven false live on mbpm2 (2026-07-28): the lane runs
    # as the SAME uid as the daemon, so anything the CLI can read, a lane with
    # arbitrary command execution can read. Until ECA-135 the whole config sat in
    # the CLI's argv, one `ps` away; it is now a turn-scoped 0600 file, one `cat`
    # away. Say it plainly: THERE IS NO LANE-TO-LANE BOUNDARY. Same uid means every
    # lane can read every other lane's file, and `state.db` holds the same
    # credentials durably — 0600 since ECA-136, which took that copy away from
    # OTHER uids but not from any lane, since every lane is this uid. ECA-135
    # changed the SHAPE
    # of the exposure — accidental capture (`ps aux` in a debugging session, which
    # scoops up every concurrent lane's config at once) became deliberate opening
    # of a named file — and shortened its life to the turn. It did not contain it.
    # The true boundary is therefore a TRUST one: grant MCP credentials only to a
    # lane you would trust with those credentials directly. The one control that
    # actually narrows this is the tool ceiling — a lane with no arbitrary execution
    # cannot reach these paths with the BUILT-IN file tools, since _path_escapes pins
    # them to the repo, and Bash is the deliberate exception. Scoped deliberately:
    # _path_escapes inspects four key names, so an MCP tool taking a path under some
    # other key would not be pinned by it (none of the granted servers has one today).
    # The shipped evolv-ultra profile grants bare Bash, so those lanes have no such
    # narrowing today. Anything stronger needs per-lane OS isolation, which the
    # daemon cannot synthesize from inside the same uid.
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
        """Whether this call is inside the ceiling. See `ceiling_denial` for why."""
        return self.ceiling_denial(tool_name, tool_input) is None

    def ceiling_denial(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """Grant-spec match; returns a denial reason, or None if the call passes.

        Three grant shapes, in the order they are tested:

        - `Tool` — bare name, every input allowed.
        - `prefix*` — a tool-NAME wildcard with no `(...)` matcher (ECA-100), the
          way to grant a whole MCP server: `mcp__jira__*` → `mcp__jira__search`.
        - `Tool(matcher)` — a command matcher, applied to `tool_input['command']`.

        **The matcher is Claude Code's syntax, applied per COMMAND (ECA-144.)** It
        used to be `command.startswith(...)` on the whole string, which is not a
        boundary at all: under a `Bash(echo*)` grant, `echo hi && cat /etc/passwd`
        passed, and so did `echoes_not_a_thing; rm -rf x`, since a raw prefix
        compare does not even respect a word boundary. Now the command is split
        into the separate commands it would run (`_split_command_segments`) and
        EVERY one of them must match some granted pattern for this tool.

        Matching follows Claude Code so that a policy written by analogy with
        `settings.json` means what its author expects (AC#3): `*` matches any run
        of characters including spaces, at any position; every other character is
        literal; a space before a trailing `*` is what makes the boundary (`ls *`
        does not match `lsof`, `ls*` does); a TRAILING `:*` is an equivalent way to
        write a trailing wildcard (`_normalize_matcher`), which matters because it
        is the form CC's own permission dialog writes; and `Tool(*)` is equivalent
        to the bare grant.

        Three carve-outs, all narrow on purpose:

        - A pattern with NO wildcard is also compared to the whole raw command,
          so a deliberate literal compound grant (`Bash(git status && npm test)`)
          works. This one is OURS, not CC parity — CC's docs say a rule must match
          each subcommand independently, and that it saves one rule per subcommand
          when a compound command is approved. It cannot smuggle anything (string
          equality against text the operator wrote out), and without it there is
          no way to grant a compound command at all.
        - A matcher grant on a tool whose input carries no `command` REFUSES,
          rather than treating the absent field as an empty string. `Read(*)`
          therefore denies instead of allowing every Read — a behaviour change,
          and the fail-closed one: the alternative is a grant that looks like a
          path filter and silently is not (there is no path matcher here; pin
          file tools with a bare `Read` plus the cwd pin).
        - The matcher does NOT strip command wrappers, which Claude Code does
          (`timeout`, `time`, `nice`, `nohup`, `xargs`, a leading `VAR=value`).
          `timeout 30 uv run pytest` is therefore outside a `Bash(uv *)` grant.
          Deliberate: every wrapper stripped is a rule about what a wrapper does,
          and `xargs`-class wrappers run arbitrary commands. Grant the wrapper
          form explicitly if a lane needs it.
        """
        patterns: list[str] = []
        for spec in self.allowed_tools:
            base, _, matcher = spec.partition("(")
            base = base.strip()
            if not matcher and base.endswith("*"):
                if tool_name.startswith(base[:-1]):
                    return None
                continue
            if base != tool_name:
                continue
            if not matcher:  # bare tool name: all inputs
                return None
            pattern = matcher.rstrip(")").strip()
            if pattern == "*":  # CC parity: `Tool(*)` is the bare grant
                return None
            patterns.append(_normalize_matcher(pattern))

        if not patterns:
            return f"tool {tool_name!r} is outside this worker's ceiling"

        raw = tool_input.get("command")
        command = raw if isinstance(raw, str) else ""
        if not command.strip():
            return (
                f"tool {tool_name!r} is granted only by command matcher, and this call "
                f"carries no command to match ({_clip(raw)})"
            )

        # Literal (wildcard-free) grant of the whole command, incl. a compound one.
        if any("*" not in p and command.strip() == p.strip() for p in patterns):
            return None

        try:
            segments = _split_command_segments(command)
        except UnparseableCommand as e:
            # AC#2: refuse what we cannot account for. A ceiling that guesses is
            # not a ceiling — and every other such question in this daemon has
            # settled the same way (see `_run_guard_hook`'s derivation refusals).
            return f"command could not be parsed, so it is refused: {e} ({_clip(command)})"
        if not segments:
            return f"command parsed to no commands, so it is refused ({_clip(command)})"

        for segment in segments:
            if not any(_glob_matches(p, segment) for p in patterns):
                return (
                    f"{tool_name} command {_clip(segment)} is outside this worker's "
                    f"ceiling (granted: {', '.join(sorted(patterns))})"
                )
        return None


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

    On CLI 2.1.165 AskUserQuestion was a UI tool, not a permission-gated one:
    it never reached can_use_tool and errored headless ("stream closed"). Hooks
    fire for every tool use, so the bridge lives here; the hook's deny reason
    carries the answer back (the eck-dev contract, relocated). Not re-measured on
    the current 2.1.220 bundle (ECA-138), and it does not need to be: the hook
    path is correct whether or not the tool also became permission-gated.
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


def _clip(value: object, limit: int = 120) -> str:
    """`repr` of an untrusted value, bounded — for messages and event records.

    The refused `script` is echoed twice per denied call: into the `tool_denied` record
    and into the message the model reads. Unbounded, a 1MB policy value becomes a 1MB
    reason in both (measured in review, not imagined). `repr` first, so control characters
    and newlines are escaped rather than embedded, then clip.
    """
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"…[+{len(text) - limit} chars]"


async def _run_guard_hook(
    repo_root: Path, script: object, tool_name: str, tool_input: dict[str, Any]
) -> tuple[str, str]:
    """Run a repo .claude/hooks guard with the hook JSON contract on stdin.

    Returns (decision, reason): decision in allow/deny/ask/error/none/refused.

    `script` arrives from the control socket (`server._dispatch` -> `WorkerPolicy.
    guard_hooks` -> `can_use_tool`) with no validation between there and here, and this
    function then hands it to `bash`. That made it an EXECUTE primitive (ECA-140): until
    the guard below, `--guard-hook Bash=../../../../tmp/x.sh` ran `/tmp/x.sh`, and a bare
    ABSOLUTE path did the same with no `..` in it at all, because `Path.__truediv__`
    discards its left operand entirely when the right one is absolute. Both shapes were
    demonstrated executing before the fix, and both are covered by tests that assert on a
    sentinel FILE rather than on a return value — a decision string cannot tell you
    whether a subprocess ran.

    Typed `object`, not `str`, for the reason `names.require_safe_worker_name` is: a
    non-str genuinely arrives here (control-socket JSON is uncoerced) and is genuinely
    handled. Annotating `str` would be a claim the runtime does not make.

    Severity, so nobody re-derives it wrongly from the word "execute": the socket is
    local-only and same-uid, and a caller who can set a worker policy can already ask for
    a bare `Bash` grant, which reaches arbitrary execution by a shorter route. What this
    closes is the REVIEWABILITY gap — running a script from outside the operator-owned
    repo tree while looking like a policy setting rather than a tool grant.
    """
    hooks_dir = repo_root / ".claude" / "hooks"
    # AC#1: refuse at PATH DERIVATION, ahead of the join, not at the caller. Validating
    # in `can_use_tool` would leave this function unsafe for its next caller, which is
    # the mistake ECA-135 made at `Engine.spawn` and ECA-137 had to correct.
    if not is_safe_hook_script(script):
        return "refused", (
            f"guard-hook script {_clip(script)} is not a plain filename component: it "
            f"must match {HOOK_SCRIPT_RE.pattern} and contain no '..' (the value is "
            "joined under <repo>/.claude/hooks/ and executed)"
        )
    hook_path = hooks_dir / script
    # ONE try around both filesystem probes, and that is not tidiness (review finding).
    # `Path.exists()` is not the total predicate it reads as: on 3.11 it re-raises any
    # OSError outside (ENOENT, ENOTDIR, EBADF, ELOOP), so `.claude/hooks` at mode 000
    # raises PermissionError straight out of `can_use_tool` — which ECA-135 established
    # kills the lane's runner loop, and `engine.py` then retries once and fails the turn.
    # Wrapping only `resolve()` (the first version of this fix) left that hole open one
    # line above the guard whose docstring invokes the never-raise invariant.
    try:
        if not hook_path.exists():
            # Deliberately UNCHANGED, and deliberately not "deny" (ECA-140). A well-formed
            # hook that is absent is a deployment mismatch, not an attack: the same caller
            # that named the hook could have named none, so failing open crosses no
            # boundary the caller was not already on the safe side of. Denying would turn
            # one stale policy row into a lane that cannot use the tool at all, and
            # reporting it would emit an event per tool call for as long as the mismatch
            # lasts. The REFUSAL path above is what AC#2 is about, and that one denies.
            return "none", f"guard hook missing: {_clip(script)}"
        # A plain filename component cannot escape `hooks_dir` lexically — but a SYMLINK
        # at that name can, and AC#3 asserts on the DIRECTORY rather than on the string.
        # Resolve both sides so a symlinked `.claude` or `hooks` is still contained; only
        # a hook FILE pointing out is refused. No hook file or hooks directory in any live
        # worker repo on either host is a symlink (checked, not assumed).
        #
        # Two residuals, disclosed rather than implied away. A HARD link shares an inode
        # and has no target to resolve, the same limit ECA-135 recorded for `O_NOFOLLOW`.
        # And there is a TOCTOU window between this check and the exec below: the file can
        # be swapped for an out-of-tree symlink in between. Both need write access to the
        # operator's repo, which a lane with `Write` already has under its own root — and
        # such a lane can equally overwrite a legitimate hook's CONTENTS, so neither is a
        # boundary this check ever claimed to hold. It contains the control-socket STRING.
        contained = hook_path.resolve(strict=True).is_relative_to(hooks_dir.resolve())
    except OSError as e:
        # `refused`, not `error`: `error` is the caller's "the hook ran and went wrong"
        # branch and its message says so, which would be false here. Deny rather than
        # fail open — a configured guard we could not even evaluate is the one case where
        # allowing really would be the silent fail-open AC#2 is about.
        return "refused", (
            f"guard-hook script {_clip(script)} could not be checked: "
            f"{e.__class__.__name__} on {str(hooks_dir)!r}"
        )
    if not contained:
        return "refused", (
            f"guard-hook script {_clip(script)} resolves outside {str(hooks_dir)!r} "
            "(symlinked out of the hooks directory)"
        )
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


def _ceiling_or_cwd_denial(
    repo_root: Path, policy: WorkerPolicy, tool_name: str, tool_input: dict[str, Any]
) -> str | None:
    """The two PURE policy checks, shared by the PreToolUse hook and can_use_tool.

    Returns a denial reason, or None if the call passes both. Both callers evaluate
    these because they are side-effect-free and idempotent — see `make_policy_hook`
    for why the hook is the one that has to run, and `make_gate` for why the gate
    keeps them anyway.
    """
    denial = policy.ceiling_denial(tool_name, tool_input)
    if denial is not None:
        return denial
    offending = _path_escapes(repo_root, tool_input)
    if offending is not None:
        return f"path {offending!r} escapes the worker root {str(repo_root)!r}"
    return None


def make_policy_hook(
    *,
    worker: str,
    repo_root: Path,
    policy: WorkerPolicy,
    events: EventLog,
    turn_id: int,
):
    """PreToolUse policy enforcement for EVERY tool call (ECA-142).

    Why this exists at all. `can_use_tool` is not the total gate the ADR-0005 shape
    assumed: the pinned SDK says so in its own contract (types.py:1929-1945, 0.2.128)
    — it is "invoked when the CLI's permission rules evaluate to 'ask'", and never for
    a call the CLI has already decided. The CLI decides some itself: measured on
    2.1.220, a read-only Bash command scoped INSIDE the session cwd (`ls -a`,
    `cat MARKER.txt`, `echo hi`) executes without the callback ever being invoked. That
    is not configuration and cannot be switched off from here — it is unchanged by
    `permission_mode='manual'`, by `setting_sources=[]`, and by running with an
    isolated `CLAUDE_CONFIG_DIR` holding no user settings at all. So for that subset
    the per-worker tool ceiling and any guard hook were simply not enforced.

    Hooks fire for every tool use, which is the SDK's own documented remedy ("to
    observe or gate *every* tool call regardless of permission rules, use a PreToolUse
    hook"). This hook therefore carries the policy that must be total.

    It returns NO decision when the call passes. That matters twice: an `allow`
    decision would skip `can_use_tool` for everything (the same bug from the other
    side), and staying silent leaves the CLI's own auto-approval intact, so the
    subset above keeps executing without a prompt exactly as before — only now the
    ceiling and the guard hooks apply to it.

    AskUserQuestion is skipped here because `make_question_hook` owns that tool and
    must be the only thing that answers for it. Note what that does NOT rest on: the
    ceiling would in fact REFUSE it for a narrow policy (`ceiling_allows` is False for
    a `Bash(echo*)` grant), and `_ALWAYS_BASE_TOOLS` puts it in every lane's `--tools`
    regardless of grant. So the escalation channel is deliberately outside the ceiling
    for every lane — an earlier draft of this docstring said the ceiling "never governs
    it", which confused a tool EXISTING with a tool being permitted. `tool_name` here
    comes from the CLI's dispatcher, not the model, so it cannot be forged.

    **This hook fails CLOSED, and that is not decoration.** An exception out of an SDK
    hook callback is caught by the CLI, logged, and converted to NO DECISION — after
    which the CLI's own auto-approval runs the tool. That is the opposite of
    `can_use_tool`, where the CLI turns the same failure into a deny. Since this hook
    is the ONLY thing policing the auto-approved subset, and since it is where the
    guard hooks now run — subprocesses, filesystem probes, the part most likely to
    raise — an unhandled exception here would silently restore the exact ECA-142
    defect with no event recorded. Measured, not reasoned: with the body made to raise,
    a lane granted `Bash(echo*)` read its canary file, `can_use_tool` was never
    consulted, and no `tool_denied` was written. Hence the blanket `except` below.
    """

    def _record(reason: str) -> None:
        """Emit is best-effort; the DENY is not conditional on it.

        `EventLog.emit` is documented as not exception-free (ENOSPC, an unwritable
        logs dir, an EISDIR/ELOOP target). It was previously called before building
        the return value, so a lane that broke its own event log — one `ln -s`, since
        there is no lane-to-lane boundary here — turned every subsequent denial into a
        raise, i.e. into a silent allow. Order matters more than the record does.
        """
        try:
            events.emit(
                worker, "tool_denied", turn_id=turn_id, tool=tool_name_seen[0],
                reason=reason, layer="pretooluse",
            )
        except Exception:  # noqa: BLE001 - a lost record must never lose the deny
            pass

    tool_name_seen = [""]

    async def on_pre_tool_use(hook_input: Any, tool_use_id: str | None, context: Any):
        try:
            data = hook_input or {}
            tool_name = data.get("tool_name") or ""
            tool_input = data.get("tool_input") or {}
            tool_name_seen[0] = tool_name
            if tool_name == "AskUserQuestion":
                return {}

            reason = _ceiling_or_cwd_denial(repo_root, policy, tool_name, tool_input)
            if reason is None:
                # Guard hooks live HERE and only here, so they run exactly once per
                # call and — unlike before — for the CLI-auto-approved subset too.
                if not _is_mapping(policy.guard_hooks):
                    # ECA-141's wedge is closed, but silently skipping a configured
                    # security control is its own defect: record it. (Not a deny —
                    # the malformed value grants nothing, and denying every call on a
                    # bad policy row would wedge the lane a different way.)
                    _record(f"guard:skipped:malformed guard_hooks {_clip(policy.guard_hooks)}")
                    return {}
                script = policy.guard_hooks.get(tool_name)
                if script:
                    decision, guard_reason = await _run_guard_hook(
                        repo_root, script, tool_name, tool_input
                    )
                    if decision == "refused":
                        # A separate branch from the one below, because that message
                        # says "Denied by repo guard hook" and that would be FALSE
                        # here: nothing ran. The refusal cannot itself traverse —
                        # `script` reaches this path only as message and record BODY,
                        # never as a path, and the event is keyed on `worker`, which
                        # ECA-137 validates inside EventLog.
                        _record(f"guard:refused:{guard_reason}")
                        return _hook_deny(f"Denied by worker policy: {guard_reason}")
                    if decision in ("deny", "ask", "error"):
                        _record(f"guard:{decision}:{guard_reason}")
                        return _hook_deny(
                            f"Denied by repo guard hook ({decision}): {guard_reason or script}"
                        )
                return {}

            _record(reason)
            return _hook_deny(f"Denied by worker policy: {reason}")
        except Exception as e:  # noqa: BLE001 - see the docstring: no decision = allow
            reason = f"policy hook failed: {e.__class__.__name__}: {_clip(str(e))}"
            _record(reason)
            return _hook_deny(f"Denied by worker policy: {reason}")

    return on_pre_tool_use


def _hook_deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _is_mapping(value: Any) -> bool:
    """`guard_hooks` reaches here uncoerced from control-socket JSON (ECA-141 tracks
    the real fix at the construction site).

    Until then a non-dict must not raise out of this hook. Note the consequence is not
    the one ECA-135 recorded: on the pinned SDK (0.2.128) an exception here does not
    kill the runner loop, it is swallowed into "no decision", so the failure mode is a
    silent ALLOW rather than a loud wedge. Worse, not better — which is why the caller
    both guards this and records the skip.
    """
    return hasattr(value, "get")


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
    """Build the can_use_tool callback for ONE turn of ONE worker.

    This is NOT the total gate — see `make_policy_hook`, which is, and which runs
    first for every call. What remains here is the AskUserQuestion escalation channel
    plus a second, idempotent evaluation of the two pure checks. Guard hooks
    deliberately do not run here any more — they have side effects, so they must run
    exactly once, and the hook is the path that sees every call.

    Be precise about what that second evaluation is worth, because an earlier draft
    of this docstring overclaimed it. It covers the calls that reach a permission
    prompt — which are exactly the calls that were NEVER the exposed ones. The subset
    ECA-142 is about (read-only Bash inside the cwd) does not reach here at all, so
    for that subset the hook is a single point of failure with no backstop in this
    function. It fails closed for its own errors; what nothing here would detect is
    the hook silently ceasing to be dispatched at all. That residual is recorded in
    the architecture doc rather than papered over.
    """

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

        # 2/3. Tool ceiling + cwd pin, re-evaluated (the base set already restricts
        #      existence; the ceiling enforces grant matchers like Bash(uv run*) on
        #      top). The PreToolUse hook has already applied both to this call; both
        #      checks are pure, so running them again costs nothing and keeps the
        #      prompt path safe on its own. Guard hooks are NOT re-run here — see the
        #      docstring.
        reason = _ceiling_or_cwd_denial(repo_root, policy, tool_name, tool_input)
        if reason is not None:
            events.emit(
                worker, "tool_denied", turn_id=turn_id, tool=tool_name,
                reason=reason, layer="can_use_tool",
            )
            return PermissionResultDeny(message=f"Denied by worker policy: {reason}")

        return PermissionResultAllow()

    return can_use_tool
