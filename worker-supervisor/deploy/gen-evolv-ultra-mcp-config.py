#!/usr/bin/env python3
"""Generate the per-lane MCP config for the evolv-ultra worker-supervisor lanes.

ECA-100 / ECA-99. Runs ON mbpm2. Writes ~/.worker-supervisor/mcp-configs/evolv-ultra.json
(0600, NOT committed) — the file `workers spawn --mcp-config` reads.

Every server's credentials live in ITS OWN headers/env block here, materialised from the
authoritative local source. They are handed to the MCP server subprocess, NEVER merged into
the worker's process env (envbuild.FORBIDDEN scrubs MCP_API_KEY etc.; the `${MCP_API_KEY}`
placeholder in the project .mcp.json would expand to empty inside a scrubbed lane — which is
exactly why supervisor lanes have had no working MCP access until now).

Sources (never printed):
  - MCP_API_KEY (jira + confluence, localhost)  <- ~/repos/fast-mcp-claude/.env
  - langfuse / greptile  Authorization headers  <- ~/.claude.json project scopes

Servers granted (ECA-100 AC#2, feasible set):
  jira, confluence (localhost, one bearer), langfuse, greptile (http + Authorization),
  context7, playwright (stdio via npx; auth via HOME-cached OAuth / none).
Deferred (documented): fast-mcp-claude-channel (per-session sidecar, not a standalone
  server a lane can attach to) and teams direct-send (lanes report to ultra0, the bridge).
"""

from __future__ import annotations

import json
import os
import stat
import sys

HOME = os.path.expanduser("~")
FMC_ENV = os.path.join(HOME, "repos/fast-mcp-claude/.env")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
OUT_DIR = os.path.join(HOME, ".worker-supervisor/mcp-configs")
OUT = os.path.join(OUT_DIR, "evolv-ultra.json")


def read_env_value(path: str, key: str) -> str:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    raise KeyError(f"{key} not found in {path}")


def find_project_server(claude_json: dict, name: str) -> dict | None:
    """First match for a server name across every ~/.claude.json project scope."""
    for _path, pv in (claude_json.get("projects") or {}).items():
        servers = pv.get("mcpServers") or {}
        if name in servers:
            return servers[name]
    # also check the global scope
    return (claude_json.get("mcpServers") or {}).get(name)


def main() -> int:
    mcp_key = read_env_value(FMC_ENV, "MCP_API_KEY")
    with open(CLAUDE_JSON, encoding="utf-8") as f:
        cj = json.load(f)

    servers: dict[str, dict] = {
        "jira": {
            "type": "http",
            "url": "http://localhost:5472/mcp",
            "headers": {"Authorization": f"Bearer {mcp_key}"},
        },
        "confluence": {
            "type": "http",
            "url": "http://localhost:5463/mcp",
            "headers": {"Authorization": f"Bearer {mcp_key}"},
        },
        "context7": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-remote", "https://mcp.context7.com/mcp"],
        },
        "playwright": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest"],
        },
    }

    warnings: list[str] = []
    for name in ("langfuse", "greptile"):
        found = find_project_server(cj, name)
        if found is None:
            warnings.append(f"{name}: NOT found in ~/.claude.json — omitted (AC#2/#3 gap)")
            continue
        # copy verbatim (url + headers carry the server's own Authorization)
        servers[name] = found

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": servers}, f, indent=2)
    os.chmod(OUT, stat.S_IRUSR | stat.S_IWUSR)  # 0600

    # Report names only — never the secret values.
    print(f"wrote {OUT} (0600): servers = {sorted(servers.keys())}")
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
