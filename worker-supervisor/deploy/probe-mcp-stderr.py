#!/usr/bin/env python3
"""ECA-136 AC#1: does a granted lane's MCP credential ever reach the CLI's stderr,
which the supervisor captures verbatim into `stderr_tail`?

WHY THIS EXISTS AS A SCRIPT. The answer is scoped to a CLI version, so it expires.
ECA-136 closed AC#1 as "no reachable path found" against the bundled CLI the SDK
actually spawns; a bundled-CLI bump re-opens the question, and re-running this is
how you re-settle it cheaply. Run it on the host that has the real credentialed
lanes (mbpm2), from that host's own checkout, so it exercises the binary that host
would spawn.

    .venv/bin/python deploy/probe-mcp-stderr.py

WHAT IT PROBES. The supervisor hands the CLI `--mcp-config <path>` plus
`--strict-mcp-config` (engine.py `_write_mcp_config` / `strict_mcp_config`), so
every case below uses that shape. Each config embeds a FAKE sentinel in a
credential position; the probe then greps the CLI's stdout and stderr for it.
No real credential is ever handled: the sentinel's VALUE is irrelevant, only its
POSITION in the config matters.

BACKGROUND, so nobody re-derives it. The pre-ECA-135 leak was real and is
reproduced here as case `inline-json`: the CLI echoes an unparseable
--mcp-config VALUE back into its error text, and before ECA-135 that value WAS the
whole credential-bearing JSON. It is now a FILE PATH, and `_write_mcp_config`
returns either `{}` (no --mcp-config emitted at all) or `str(path)` — never a dict
— so the daemon can no longer produce that input shape. `inline-json` is kept as a
POSITIVE CONTROL: if it stops finding the sentinel, the probe has gone blind and
every other clean result below is worthless.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SENTINEL = "FAKE-SENTINEL-ECA136-NOT-A-REAL-CREDENTIAL"
TIMEOUT_S = 90


def find_cli() -> str:
    """The binary the SDK would spawn — NOT whatever `claude` is on PATH.

    `subprocess_cli._find_cli()` checks `_find_bundled_cli()` FIRST and the
    supervisor never sets `cli_path`, so the bundled binary always wins. On both
    fleet hosts that is a different version from PATH `claude`, which is exactly
    how ECA-136's first probe round ended up scoped to the wrong binary.
    """
    try:
        from claude_agent_sdk._internal.transport import subprocess_cli
    except Exception as e:  # pragma: no cover - import shape is the SDK's
        sys.exit(f"cannot import the SDK transport ({e}); run me from the project venv")
    # Mirrors SubprocessCLITransport._find_bundled_cli (a METHOD, so it cannot be
    # imported directly): <sdk package>/_bundled/claude.
    name = "claude.exe" if sys.platform == "win32" else "claude"
    bundled = Path(subprocess_cli.__file__).parent.parent.parent / "_bundled" / name
    if bundled.is_file():
        return str(bundled)
    fallback = shutil.which("claude")
    if not fallback:
        sys.exit("no bundled CLI and none on PATH; nothing to probe")
    print(f"WARNING: no bundled CLI at {bundled}; the SDK would fall back to PATH.\n"
          f"         Probing {fallback} instead — verify that is really what spawns.")
    return fallback


def cases(tmp: Path) -> list[tuple[str, list[str], str]]:
    """(label, extra argv, note). Each writes its own config under tmp."""
    out: list[tuple[str, list[str], str]] = []

    def cfg(name: str, payload) -> Path:
        p = tmp / f"{name}.json"
        p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return p

    http = {"mcpServers": {"s": {"type": "http", "url": "http://127.0.0.1:1/mcp",
                                 "headers": {"Authorization": f"Bearer {SENTINEL}"}}}}
    stdio = {"mcpServers": {"s": {"type": "stdio", "command": "/nonexistent/server",
                                  "env": {"TOKEN": SENTINEL}}}}

    # The control must be UNPARSEABLE inline text. Valid inline JSON is accepted as
    # a config and raises nothing, so there is no error message to echo — the
    # original leak needed a value the CLI failed to parse and then reported back
    # as a filename. Truncated JSON reproduces that exactly.
    out.append(("inline-unparseable (POSITIVE CONTROL — must FIND the sentinel)",
                ["--mcp-config", json.dumps(http)[:-3]],
                "the pre-ECA-135 shape: a credential-bearing dict rendered into argv"))
    out.append(("missing-file", ["--mcp-config", str(tmp / "absent.json")],
                "the daemon's shape when the path is wrong: only a PATH should echo"))
    out.append(("malformed-json", ["--mcp-config", str(cfg("malformed",
                '{"mcpServers": {"s": {"headers": {"Authorization": "Bearer '
                + SENTINEL + '"' ))], "truncated JSON with the sentinel inside"))
    out.append(("array-root", ["--mcp-config", str(cfg("array", [http]))],
                "valid JSON, wrong shape"))
    out.append(("bad-transport", ["--mcp-config", str(cfg("badtype",
                {"mcpServers": {"s": {"type": "not-a-transport",
                                      "headers": {"Authorization": f"Bearer {SENTINEL}"}}}}))],
                "schema-invalid but parseable"))
    out.append(("missing-required-field", ["--mcp-config", str(cfg("missing",
                {"mcpServers": {"s": {"type": "stdio", "env": {"TOKEN": SENTINEL}}}}))],
                "stdio with no command"))
    out.append(("unreachable-http-bearer", ["--mcp-config", str(cfg("http", http))],
                "connect failure with an auth header present"))
    out.append(("unreachable-stdio-child", ["--mcp-config", str(cfg("stdio", stdio))],
                "child that cannot exec, with a credential in its env"))

    noperm = cfg("noperm", http)
    os.chmod(noperm, 0o000)
    out.append(("permission-denied-file", ["--mcp-config", str(noperm)],
                "EACCES on a config whose contents carry the sentinel"))
    return out


def main() -> int:
    cli = find_cli()
    ver = subprocess.run([cli, "--version"], capture_output=True, text=True).stdout.strip()
    print(f"host={os.uname().nodename}  bundled CLI={ver}\n  {cli}\n")

    tmp = Path(tempfile.mkdtemp(prefix="eca136-probe-"))
    leaks, clean, errors = [], [], []
    try:
        for label, extra, note in cases(tmp):
            argv = [cli, "-p", "say OK", "--max-turns", "1",
                    "--strict-mcp-config", *extra]
            try:
                r = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=TIMEOUT_S, cwd=tmp)
                blob = (r.stdout or "") + (r.stderr or "")
                hits = blob.count(SENTINEL)
            except subprocess.TimeoutExpired:
                errors.append((label, "TIMED OUT"))
                print(f"  ??  {label}: TIMED OUT after {TIMEOUT_S}s")
                continue
            first = next((ln for ln in (r.stderr or "").splitlines() if ln.strip()), "")
            verdict = "LEAK" if hits else "clean"
            (leaks if hits else clean).append(label)
            print(f"  {verdict:5s} {label}  (exit={r.returncode}, sentinel x{hits})")
            print(f"        {note}")
            if first:
                print(f"        stderr[0]: {first[:150]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    control_leaked = any("POSITIVE CONTROL" in c for c in leaks)
    print(f"\n  leaked={len(leaks)}  clean={len(clean)}  inconclusive={len(errors)}")
    if not control_leaked:
        print("\nPROBE IS BLIND: the positive control did not echo the sentinel, so a "
              "'clean' result here proves nothing. Fix the probe before trusting it.")
        return 2
    real = [c for c in leaks if "POSITIVE CONTROL" not in c]
    if real:
        print(f"\nREACHABLE LEAK PATH(S): {', '.join(real)}\n"
              "AC#1 resolves to 'reachable' — the stderr_tail scrubber is owed.")
        return 1
    print("\nNo reachable path: every case except the positive control kept the "
          "sentinel out of stdout and stderr, on THIS CLI version.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
