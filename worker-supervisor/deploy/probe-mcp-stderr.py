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

BACKGROUND, so nobody re-derives it. The leak shape is real and is reproduced here
as case `inline-unparseable`: the CLI echoes an unparseable --mcp-config VALUE back
into its error text, and before ECA-135 the SDK put the whole credential-bearing
dict there. `_write_mcp_config` now returns either `{}` (no --mcp-config emitted at
all) or `str(path)` — never a dict — so the daemon can no longer produce that input
shape. `inline-unparseable` is kept as a POSITIVE CONTROL: if it stops finding the
sentinel, the probe has gone blind and every other clean result is worthless.

READ THE EXIT CODE, NOT THE WORD "clean". A case only counts as evidence if the
CLI actually REACHED the behaviour being probed. Four cases (bad-transport,
missing-required-field, unreachable-http-bearer, unreachable-stdio-child) need the
CLI to start a turn and attempt an MCP connection, which needs working auth — over
non-interactive SSH it usually does not have it, and the CLI exits at auth having
never touched MCP. Those runs are NOT evidence of anything, so they are reported
INCONCLUSIVE rather than clean, and any inconclusive case makes the whole run
inconclusive (exit 3). Exit codes: 0 no reachable path, 1 a leak was found,
2 the probe is blind (control did not fire), 3 inconclusive.
"""

from __future__ import annotations

import hashlib
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
    # The sha256 is what makes a run on one host transferable to another: the
    # verdict is a property of this BINARY, not of the machine. Where a host cannot
    # authenticate (plain ssh on macOS: 'Not logged in · Please run /login'), a
    # conclusive run elsewhere against an IDENTICAL hash covers the cases this run
    # had to mark inconclusive. Record the hash whenever you record a verdict.
    digest = hashlib.sha256(Path(cli).read_bytes()).hexdigest()
    print(f"host={os.uname().nodename}  bundled CLI={ver}\n  {cli}\n  sha256={digest}\n")

    tmp = Path(tempfile.mkdtemp(prefix="eca136-probe-"))
    leaks, clean, inconclusive = [], [], []
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
                inconclusive.append((label, f"timed out after {TIMEOUT_S}s"))
                print(f"  ????  {label}: TIMED OUT — not evidence")
                continue
            first = next((ln for ln in (r.stderr or "").splitlines() if ln.strip()), "")
            if hits:
                leaks.append(label)
                verdict = "LEAK"
            elif _died_before_mcp(blob):
                # No sentinel, but the CLI never got as far as the thing being
                # probed — most often auth. Silence here means nothing.
                inconclusive.append((label, "CLI exited before attempting MCP"))
                verdict = "????"
            else:
                clean.append(label)
                verdict = "clean"
            print(f"  {verdict:5s} {label}  (exit={r.returncode}, sentinel x{hits})")
            print(f"        {note}")
            if first:
                print(f"        stderr[0]: {first[:150]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  leaked={len(leaks)}  clean={len(clean)}  inconclusive={len(inconclusive)}")
    for label, why in inconclusive:
        print(f"    inconclusive: {label} — {why}")

    if not any("POSITIVE CONTROL" in c for c in leaks):
        print("\nPROBE IS BLIND: the positive control did not echo the sentinel, so a "
              "'clean' result here proves nothing. Fix the probe before trusting it.")
        return 2
    real = [c for c in leaks if "POSITIVE CONTROL" not in c]
    if real:
        print(f"\nREACHABLE LEAK PATH(S): {', '.join(real)}\n"
              "AC#1 resolves to 'reachable' — the stderr_tail scrubber is owed.")
        return 1
    if inconclusive:
        print("\nINCONCLUSIVE: the cases above never reached the behaviour they probe, "
              "so this run does NOT establish 'no reachable path'. Re-run somewhere the "
              "CLI can authenticate (an interactive session on the host, not plain ssh).")
        return 3
    print("\nNo reachable path: every case except the positive control reached the CLI's "
          "MCP handling and kept the sentinel out of stdout and stderr, on THIS version.")
    return 0


def _died_before_mcp(blob: str) -> bool:
    """Did the CLI exit before it could attempt anything MCP-related?

    A config-VALIDATION error is fine — that IS the behaviour under test, and it
    proves the CLI parsed our config. An auth/credit failure is not: it happens
    first and tells us nothing about what MCP handling would have printed.
    """
    low = blob.lower()
    if "invalid mcp configuration" in low:
        return False
    return any(s in low for s in (
        "not logged in", "invalid api key", "authentication", "unauthorized",
        "credit balance", "please run /login", "oauth",
    ))


if __name__ == "__main__":
    raise SystemExit(main())
