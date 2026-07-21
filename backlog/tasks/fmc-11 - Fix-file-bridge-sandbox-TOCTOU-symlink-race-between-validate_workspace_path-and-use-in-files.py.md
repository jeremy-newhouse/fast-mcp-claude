---
id: FMC-11
title: >-
  Fix file-bridge sandbox: TOCTOU symlink race between validate_workspace_path
  and use in files.py
status: To Do
assignee: []
created_date: '2026-07-21 14:44'
labels:
  - security
  - sandbox
dependencies: []
references:
  - backlog/docs/reviews/doc-2 - Codex-full-codebase-review-2026-07-21.md
priority: high
type: bug
ordinal: 11000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Discovered by a second-opinion full-codebase review (OpenAI Codex, gpt-5.6-sol, ultra effort) on 2026-07-21. This finding was independently re-verified against the current code in this same session (file and approximate line numbers traced against the real source), so it is confirmed, not a raw unreviewed model claim. The full adjudicated report is saved as Backlog document doc-2, "Codex full-codebase review (2026-07-21)", if broader context is ever needed, but this description is self-contained.

The bug: validate_workspace_path in utils/validation.py (around lines 165-186) canonicalizes a caller-supplied path exactly once, using Path.resolve(strict=False) followed by a relative_to() containment check against the configured workspace roots allowlist, and returns that single resolved Path object. Every caller of this function then performs a separate, LATER filesystem operation on that same already-resolved Path, with no file-descriptor-based re-check and no no-follow protection at the point of actual use:

- list_files in tools/files.py (around lines 36-41) validates the path, then later calls os.scandir on the resolved path to enumerate entries.
- read_file in tools/files.py (around lines 80-95) validates the path, then later calls stat and read_text/read_bytes on the resolved path.
- write_file in tools/files.py (around lines 136-149) validates the path, then later calls mkdir(parents=True, exist_ok=True) on the resolved path's parent and write_bytes on the resolved path itself.

Because the resolved Path returned by validate_workspace_path is just a string-like path, not an open file descriptor, every one of these later filesystem calls re-traverses the path from scratch at the OS level. This creates a classic time-of-check-to-time-of-use (TOCTOU) race: a local process on the same machine (for example another workspace tenant, or a compromised dependent process running on the same host) can, in the brief window between validate_workspace_path returning and the tool's actual filesystem operation running, delete a path component that existed and passed containment at check time and replace it with a symlink pointing outside the workspace roots allowlist (for example toward a system directory). The subsequent scandir, stat/read, or mkdir/write call then follows the newly swapped symlink and operates outside the intended sandbox, even though the path was confirmed contained at the moment it was checked.

This is explicitly a distinct, still-open issue from the already-completed task FMC-4. FMC-4 fixed a check-ORDER bug: the existence check used to run before the containment check, which leaked (via error type or message) whether an out-of-sandbox path existed. In the course of that fix, FMC-4 confirmed the symlink-resolve logic is sound for symlinks that already exist at the moment of the check. FMC-4 did not address, and its own notes do not claim to address, this separate race window between when the check happens and when the filesystem operation actually happens later in each tool. A future implementer should not treat FMC-4 as having already closed this gap.

Why it matters: this file bridge is explicitly a controller-facing sandboxed capability that lets a remote peer read and write files on this machine, gated only by the workspace roots allowlist described in this project's security model. An attacker able to win this race (a co-located process, another tenant sharing the host, or a compromised dependency in the same repo tree) can turn a nominally sandboxed read or write into an arbitrary read or write anywhere the server process can reach on the host, defeating the sandbox containment guarantee entirely, for any of the three file-bridge tools.

A correct fix needs either operating relative to an opened root directory descriptor with a beneath-only, no-symlink-follow resolution (opening path components with a directory file descriptor and no-follow semantics so an attacker cannot substitute a symlink after the check), or an equivalent descriptor-based re-verification performed immediately before each actual filesystem operation (for example opening the final target with no-follow semantics, then confirming via the resulting descriptor that its real path is still inside the workspace roots allowlist before doing any further I/O through that descriptor rather than through the path string a second time). Whichever approach is chosen, it needs to cover all three call sites: the directory scan in list_files, the stat-plus-read in read_file, and the mkdir-plus-write in write_file (including write_file's parent-directory creation step, which is itself a second traversal that can be raced).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 list_files no longer allows a directory that was swapped for a symlink pointing outside WORKSPACE_ROOTS between path validation and the directory scan to actually be listed; the operation fails closed instead of scanning through the swapped symlink.
- [ ] #2 read_file no longer allows a file whose path or one of whose parent directories was swapped for a symlink pointing outside WORKSPACE_ROOTS between path validation and the actual file open/read to actually be read; the operation fails closed instead of returning content from outside the sandbox.
- [ ] #3 write_file no longer allows a target path or a to-be-created parent directory that was swapped for a symlink pointing outside WORKSPACE_ROOTS between path validation and the actual mkdir/write to actually be written through; the operation fails closed instead of writing outside the sandbox.
- [ ] #4 All three scenarios above (list_files, read_file, write_file) are covered by tests that simulate the race by substituting a symlink for a path component after validation but before the tool's real filesystem operation, and assert each operation is rejected rather than succeeding outside the sandbox.
<!-- AC:END -->
