---
id: FMC-8
title: >-
  Verify and fix .mcp.json.example channel server-key mismatch vs channel.py's
  hardcoded SERVER_NAME
status: Done
assignee:
  - '@claude'
created_date: '2026-07-20 20:26'
updated_date: '2026-07-20 23:11'
labels:
  - channels
  - config
dependencies: []
priority: medium
type: bug
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by an ad-hoc agent-team dogfooding review (2026-07-20) of the repo's documentation and config examples.

.mcp.json.example names the channel MCP server entry "claude-channel", and README.md:142 tells users to launch with "--dangerously-load-development-channels server:claude-channel". But channel.py hardcodes SERVER_NAME = "fast-mcp-claude-channel", and channel.py:98-99 auto-allows the channel's own reply tool by that exact fully-qualified name: mcp__fast-mcp-claude-channel__reply.

Under the example's key, the reply tool would register as mcp__claude-channel__reply instead, which would not match the hardcoded auto-allow entry. start-session.sh:192 already uses the correct "fast-mcp-claude-channel" key, so the production path may be unaffected — this needs to be verified specifically against someone following .mcp.json.example + README.md literally, since that is a documented, user-facing path.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Confirmed whether the mcp.json.example channel server key mismatch actually breaks the reply tool auto-allow when followed literally
- [x] #2 If broken: the example key matches channel.py SERVER_NAME, or the auto-allow logic is made robust to the configured server name, with a test or documented manual repro
- [ ] #3 If not exploitable in practice: the reasoning is recorded on the task and no functional change is made
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Determine ground truth for AC#1: how does Claude Code actually name/prefix MCP tools
   from a stdio server registered in .mcp.json — by the server's self-declared
   `serverInfo.name` (sent in the MCP initialize handshake) or by the local .mcp.json
   config key the user chose? channel.py's own comment (line 97-98) asserts
   "MCP server name == the .mcp.json key", but that's an unverified in-code assumption,
   not proof.
2. Verify via two independent sources: (a) dispatch a research subagent against official
   Claude Code docs (permissions.md, mcp.md, channels-reference.md); (b) directly inspect
   the installed `claude` CLI binary's extracted strings for the tool-naming logic
   (`mcp__${serverName}__` construction, and where `serverName` is populated from
   `Object.entries(mcpServers)` — i.e. the config key).
3. Both sources agree: Claude Code prefixes tools as `mcp__<.mcp.json-key>__<tool>`, using
   the config key, never the server's self-declared name. So .mcp.json.example's key
   "claude-channel" vs channel.py's hardcoded SERVER_NAME "fast-mcp-claude-channel" IS a
   real, exploitable mismatch when the example is followed literally: OUR_REPLY_TOOL
   ("mcp__fast-mcp-claude-channel__reply") would not match the actual registered tool
   name ("mcp__claude-channel__reply"), so the channel's own reply call (and
   send_teams/list_sessions/send_to_session/check_session_message) would fall through
   OUR_TOOLS's always-allow check into the normal approval-routing path instead —
   breaking the agent's only reply mechanism when literally following the docs. AC#1
   resolves to "confirmed broken"; proceed under AC#2's branch (fix), not AC#3's
   (no-op).
4. Fix scope: align every documented/example config key with channel.py's hardcoded
   SERVER_NAME, rather than making SERVER_NAME dynamic/configurable (SERVER_NAME backs
   the low-level `Server(SERVER_NAME, ...)` declaration itself and 5 OUR_*_TOOL
   constants; changing it to read from CLI/env would be a larger, riskier behavior
   change than fixing 4 doc/config files to be consistent):
   - .mcp.json.example: rename the "claude-channel" entry key to "fast-mcp-claude-channel"
     and update the _comment to explain why the key must match SERVER_NAME exactly.
   - README.md: fix the `--dangerously-load-development-channels server:claude-channel`
     example and the "Add the `claude-channel` entry" prose; add a one-line note on why
     the key must match.
   - .claude/commands/worker.md: fix the same dev-channels flag example.
   - .claude/settings.example.json: fix the "_comment" field's cross-reference to the
     .mcp.json channel key.
5. Add regression tests (tests/test_channel.py) that parse .mcp.json.example, README.md,
   and worker.md at test time and assert their channel-adapter key/flag equals
   channel_mod.SERVER_NAME, so this can't silently drift apart again.
6. Verify: uv run pytest tests/test_channel.py -v (new tests pass), full uv run pytest,
   uv run ruff check src/ tests/.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Verified AC#1 via two independent sources, both agreeing: Claude Code prefixes MCP tool
names as mcp__<.mcp.json-config-key>__<tool>, using the LOCAL config key the operator
chose, never the server's self-declared MCP `serverInfo.name`. (a) A claude-code-guide
research subagent cited official docs: permissions.md ("MCP rules use the server name as
configured in Claude Code"), mcp.md line 314 (plugin tool names embed "the server key"),
and channels-reference.md (`--dangerously-load-development-channels server:<name>` takes
the .mcp.json key). (b) Direct inspection of the installed claude CLI binary (v2.1.216)
via `strings` confirmed the same in the compiled source: tool names are built as
`mcp__${Dc(serverName)}__${Dc(toolName)}`, and serverName is populated by iterating
`Object.entries(mcpServers)` over the .mcp.json config object — i.e. literally the config
key, not anything from the initialize handshake.

This confirms the bug is real: .mcp.json.example's channel entry key was "claude-channel"
while channel.py hardcodes SERVER_NAME = "fast-mcp-claude-channel" (used to build
OUR_REPLY_TOOL etc. for the permission-relay's always-allow check on the adapter's own
delivery-path tools). Someone following .mcp.json.example + README.md literally would
register the tool as mcp__claude-channel__reply, which would NOT match
mcp__fast-mcp-claude-channel__reply in OUR_TOOLS, so the channel's reply call would fall
through into the normal (non-owner) permission-routing path — breaking the agent's only
reply mechanism (invariant 9 in CLAUDE.md) for anyone following the documented example
verbatim. start-session.sh already used the correct key, so the production/documented
launch path via that script was unaffected — only the literal .mcp.json.example + README
path was broken.

Fix: aligned every documented/example config key with channel.py's hardcoded SERVER_NAME
(chose this over making SERVER_NAME dynamic, since it backs the low-level Server(...)
declaration + 5 OUR_*_TOOL constants and dynamic config would be materially riskier for a
narrow doc/config-parity bug): .mcp.json.example (key rename + comment), README.md (flag
example + prose + a new explanatory line), .claude/commands/worker.md (flag example),
.claude/settings.example.json (comment cross-reference). Added 3 regression tests in
tests/test_channel.py that parse .mcp.json.example/README.md/worker.md at test time and
assert the channel key/flag equals channel_mod.SERVER_NAME, so this can't silently drift
apart again.

Verified: uv run pytest tests/test_channel.py -v -> 92 passed (incl. the 3 new tests);
full uv run pytest -> 257 passed; uv run ruff check src/ tests/ -> All checks passed.

AC#3 ("if not exploitable, no functional change") does not apply — AC#1's finding was
"confirmed broken," so the task proceeds under AC#2's branch, not AC#3's. Left AC#3
unchecked as not applicable to this outcome (not failed, mutually exclusive with AC#2).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Confirmed (AC#1) via two independent sources — official Claude Code docs and direct
inspection of the installed claude CLI binary's compiled source — that Claude Code
prefixes MCP tool names using the .mcp.json config KEY, never the server's self-declared
name. This meant .mcp.json.example's channel entry key ("claude-channel") really did
mismatch channel.py's hardcoded SERVER_NAME ("fast-mcp-claude-channel"), breaking the
permission relay's always-allow check for the channel's own reply/send_teams/etc. tools
(mcp__claude-channel__reply != mcp__fast-mcp-claude-channel__reply) for anyone following
.mcp.json.example + README.md literally — start-session.sh's generated config already
used the correct key, so only the literal documented path was affected.

Fixed (AC#2) by aligning every documented/example config key with channel.py's SERVER_NAME
rather than making SERVER_NAME dynamic (narrower, lower-risk change): .mcp.json.example,
README.md, .claude/commands/worker.md, .claude/settings.example.json. Added 3 regression
tests in tests/test_channel.py that parse those files and assert the channel key/flag
equals channel_mod.SERVER_NAME.

Verified: uv run pytest tests/test_channel.py -v (92 passed), full uv run pytest (257
passed), uv run ruff check src/ tests/ (clean).
<!-- SECTION:FINAL_SUMMARY:END -->
