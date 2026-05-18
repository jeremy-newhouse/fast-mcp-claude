#!/usr/bin/env bash
# PreToolUse hook — relays permission decisions to a remote controller via fast-mcp-claude.
#
# Install fast-mcp-claude globally (`uv tool install /path/to/fast-mcp-claude`) so
# that `fast-mcp-claude-hook` is on PATH, then add this hook to your project's
# .claude/settings.json — see .claude/settings.example.json for a template.
#
# Configuration via env vars (see src/fast_mcp_claude/hook.py docstring):
#   CRM_LOCAL_URL         — default http://127.0.0.1:5473/mcp
#   MCP_API_KEY           — bearer for the local server (required if server demands it)
#   CRM_DECISION_TIMEOUT  — total seconds to wait for the controller (default 300)
#   CRM_AUTO_PASS_TOOLS   — comma-separated tools to skip (e.g. "Read,Glob,Grep")
#   CRM_HOOK_DEBUG=1      — stderr diagnostics

set -e
exec fast-mcp-claude-hook
