---
id: FMC-11
title: >-
  Fix file-bridge sandbox: TOCTOU symlink race between validate_workspace_path
  and use in files.py
status: Done
assignee:
  - '@jeremy'
created_date: '2026-07-21 14:44'
updated_date: '2026-07-21 18:12'
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
- [x] #1 list_files no longer allows a directory that was swapped for a symlink pointing outside WORKSPACE_ROOTS between path validation and the directory scan to actually be listed; the operation fails closed instead of scanning through the swapped symlink.
- [x] #2 read_file no longer allows a file whose path or one of whose parent directories was swapped for a symlink pointing outside WORKSPACE_ROOTS between path validation and the actual file open/read to actually be read; the operation fails closed instead of returning content from outside the sandbox.
- [x] #3 write_file no longer allows a target path or a to-be-created parent directory that was swapped for a symlink pointing outside WORKSPACE_ROOTS between path validation and the actual mkdir/write to actually be written through; the operation fails closed instead of writing outside the sandbox.
- [x] #4 All three scenarios above (list_files, read_file, write_file) are covered by tests that simulate the race by substituting a symlink for a path component after validation but before the tool's real filesystem operation, and assert each operation is rejected rather than succeeding outside the sandbox.
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add src/fast_mcp_claude/utils/secure_fs.py: root-anchored dir_fd walk (_walk_to_parent_fd) opening every remaining path component with O_NOFOLLOW, plus secure_scandir/secure_open_read/secure_open_write built on it (verified os.open/os.mkdir dir_fd + O_NOFOLLOW semantics empirically on this platform first, since os.scandir does NOT support dir_fd on macOS -- confirmed via a standalone probe script before writing any fix code).
2. Rewrite list_files/read_file/write_file in tools/files.py to perform their real filesystem operation through these primitives instead of re-traversing the validated Path string; translate the resulting OSError into PermissionDeniedError (fail closed), keep the existing early ValidationError checks for genuine non-race bad-input cases (not-a-directory/not-a-file/is-a-directory).
3. write_file's overwrite=False path switches from a separate racy exists()-then-write check to atomic O_EXCL; the parent-directory auto-create step also walks via dir_fd/O_NOFOLLOW so it cannot be raced either (explicitly called out in the task description).
4. Add tests/test_files.py: primitive-level TOCTOU simulations (swap a component for a symlink right after computing the validated path, before calling the secure_* helper) for all three call sites plus the parent-mkdir race and O_EXCL atomicity, and tool-level TOCTOU simulations (monkeypatch validate_workspace_path to inject the swap between check and use) asserting a specific PERMISSION_DENIED code, plus happy-path regression tests.
5. Confirm the 4 tool-level regression tests fail against the pre-fix files.py via git stash, then restore the fix; run full suite + ruff.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Verified fix empirically before coding: os.scandir does NOT support dir_fd on macOS Darwin (os.scandir in os.supports_dir_fd is False) but os.open/os.mkdir do, and os.scandir accepts a raw fd as its path arg -- confirmed via a standalone probe script exercising the exact dir_fd+O_NOFOLLOW walk, an O_NOFOLLOW leaf-symlink open (fails ELOOP), O_EXCL atomicity, and mkdir+dir_fd, all on this Python 3.12.9/macOS install rather than assumed from memory (same discipline FMC-14 required).

Implementation: new utils/secure_fs.py provides secure_scandir/secure_open_read/secure_open_write, each walking from an os.open()'d workspace-root fd and opening every remaining path component (including any newly-created parent directories) with O_NOFOLLOW via dir_fd -- so a symlink substituted at any level after validate_workspace_path returns raises OSError instead of being followed. write_file's overwrite=False now uses O_EXCL for an atomic create-or-fail instead of a separate racy exists() check (a free correctness improvement, not just for the symlink case). Rewrote list_files/read_file/write_file in tools/files.py to route their real filesystem operation through these primitives, translating the resulting OSError into PermissionDeniedError while preserving the existing ValidationError checks for genuine (non-race) bad-input cases.

Verification: added tests/test_files.py (14 tests) -- 6 direct unit tests of the secure_fs primitives simulating the race (swap a component for a symlink after computing the validated path, before the real op), covering list_files/read_file/write_file's target, an ANCESTOR directory swap, the write_file parent-mkdir race explicitly called out in the task description, and O_EXCL atomicity; 4 tool-level TOCTOU tests (monkeypatch validate_workspace_path to inject the swap between check and use) asserting a specific error.code == PERMISSION_DENIED (not a bare exception); 4 happy-path regression tests confirming normal list/read/write/overwrite=false behavior is unchanged. Confirmed via git stash (of tools/files.py only) that all 4 tool-level TOCTOU tests fail against the pre-fix code with success=True (i.e. the attack actually succeeds pre-fix), then restored the fix -- all 14 pass. Full suite: 315 passed (up from 301). ruff check clean; ruff format applied to the 2 files this branch touches (files.py, test_files.py) -- the 9 pre-existing-drift files already documented in FMC-4/6/8/14 are untouched and still drift. Also updated CLAUDE.md's module layout + File-bridge sandbox security-model bullet to document utils/secure_fs.py.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Fixed the file-bridge TOCTOU: validate_workspace_path only proved containment at check time and returned a plain Path, so list_files/read_file/write_file's later os.scandir/read/write calls re-traversed that path string and could be redirected by a symlink swapped in during the gap. Added utils/secure_fs.py (secure_scandir/secure_open_read/secure_open_write), which walk from an already-open workspace-root directory fd and open every remaining path component -- including newly-created parent directories in write_file -- with O_NOFOLLOW via dir_fd, so a swapped symlink at any level raises OSError instead of being followed; write_file's overwrite=False also became atomic (O_EXCL) instead of a separate racy exists() check. Verified via a standalone empirical probe (os.scandir does not support dir_fd on this macOS install, but os.open/os.mkdir do, and scandir accepts a raw fd) before writing any fix code. Added tests/test_files.py: 6 primitive-level TOCTOU simulations (target dir, ancestor dir, target file for read and write, the parent-mkdir race, O_EXCL atomicity), 4 tool-level TOCTOU simulations asserting a specific PERMISSION_DENIED error code, and 4 happy-path regressions. Confirmed via git stash that all 4 tool-level tests fail (attack succeeds, success=True) against the pre-fix code and pass after the fix. Full suite 315 passed (up from 301); ruff check clean; ruff format applied to the 2 touched files only. All 4 ACs verified with this evidence.
<!-- SECTION:FINAL_SUMMARY:END -->
