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
  - MCP_API_KEY (jira + confluence, localhost)     <- ~/repos/fast-mcp-claude/.env
  - langfuse Authorization (Basic pk/sk, AWS dev)  <- ~/.claude.json project scope
  - greptile Authorization (Bearer API key)        <- ~/.worker-supervisor/secrets/greptile.token

Servers granted (ECA-100 AC#2, feasible set):
  jira, confluence (localhost, one bearer), langfuse (AWS-dev http, Basic), greptile
  (api.greptile.com http, Bearer — operator key, NOT the built-in plugin), context7 (stdio npx).
Deferred (documented): playwright (flaky npx stdio + unused by any evolv-ultra skill —
  browser-test is Bash/CLI-driven; disabled per operator 2026-07-11); fast-mcp-claude-channel
  (per-session sidecar, not attachable to a supervisor lane); teams direct-send (lanes report
  to ultra0, the Teams bridge).
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from urllib.parse import urlsplit, urlunsplit

HOME = os.path.expanduser("~")
FMC_ENV = os.path.join(HOME, "repos/fast-mcp-claude/.env")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
GREPTILE_TOKEN_FILE = os.path.join(HOME, ".worker-supervisor/secrets/greptile.token")
OUT_DIR = os.path.join(HOME, ".worker-supervisor/mcp-configs")
OUT = os.path.join(OUT_DIR, "evolv-ultra.json")


def collapse_path_slashes(url: str) -> str:
    """Collapse '//' runs in the PATH only (netloc untouched) — the langfuse dev def
    carries 'evolv-ultra.com//api/...' which 308-redirects; hit the canonical path."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, re.sub(r"/{2,}", "/", p.path), p.query, p.fragment))


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
    }

    warnings: list[str] = []

    # langfuse: AWS-dev hosted MCP; lift the def (Basic pk/sk auth) from ~/.claude.json
    # and normalise the double-slash path (dev returns 308 on '//api/...').
    langfuse = find_project_server(cj, "langfuse")
    if langfuse is None:
        warnings.append("langfuse: NOT found in ~/.claude.json — omitted (AC#2/#3 gap)")
    else:
        if langfuse.get("url"):
            langfuse["url"] = collapse_path_slashes(langfuse["url"])
        servers["langfuse"] = langfuse

    # greptile: use the operator-provided API key (0600 secrets file), NOT the
    # built-in greptile plugin (its OAuth expired). Direct api.greptile.com MCP.
    if os.path.exists(GREPTILE_TOKEN_FILE):
        with open(GREPTILE_TOKEN_FILE, encoding="utf-8") as f:
            gkey = f.read().strip()
        servers["greptile"] = {
            "type": "http",
            "url": "https://api.greptile.com/mcp",
            "headers": {"Authorization": f"Bearer {gkey}"},
        }
    else:
        warnings.append(
            f"greptile: {GREPTILE_TOKEN_FILE} missing — omitted (write the API key there, 0600)"
        )

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
