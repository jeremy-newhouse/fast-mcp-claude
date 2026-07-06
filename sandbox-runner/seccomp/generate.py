"""Generate eca-seccomp.json from the pinned moby default (see README.md).

Run from this directory:  python3 generate.py
"""

import copy
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
base = json.loads((_HERE / "moby-default-base.json").read_text())

# Extra hardening delta beyond --cap-drop=ALL/--no-new-privileges: syscalls the
# python+git+CLI workload never needs, hard-denied (fall through to default
# SCMP_ACT_ERRNO) regardless of any capability. NOTE: clone/clone3 are NOT here
# (fork/pthread need clone; the base's conditional group already blocks the
# namespace-creating variants when CAP_SYS_ADMIN is absent).
TIGHTEN_DENY = {
    # debugging / cross-process introspection
    "ptrace", "process_vm_readv", "process_vm_writev", "kcmp",
    # persona / ASLR games
    "personality",
    # mount / fs-namespace family
    "mount", "umount2", "pivot_root", "chroot", "mount_setattr",
    "move_mount", "open_tree", "fsopen", "fsconfig", "fsmount", "fspick",
    # namespace create/join
    "unshare", "setns",
    # kernel keyring
    "keyctl", "add_key", "request_key",
    # bpf / perf / io ports / accounting / quotas / modules / swap / kexec
    "bpf", "perf_event_open", "ioperm", "iopl", "acct", "quotactl",
    "nfsservctl", "swapon", "swapoff", "kexec_load", "kexec_file_load",
    "init_module", "finit_module", "delete_module",
}

out = copy.deepcopy(base)
new_groups = []
removed = set()
for g in out["syscalls"]:
    names = [n for n in g.get("names", [])]
    kept = [n for n in names if n not in TIGHTEN_DENY]
    dropped = [n for n in names if n in TIGHTEN_DENY]
    removed.update(dropped)
    if not kept:
        continue  # whole group was hardening-denied -> drop it (falls to ERRNO)
    g = dict(g)
    g["names"] = kept
    new_groups.append(g)
out["syscalls"] = new_groups

json.dump(out, open("eca-seccomp.json", "w"), indent=2)
open("eca-seccomp.json", "a").write("\n")

# report
allow = set()
for g in out["syscalls"]:
    if g.get("action") == "SCMP_ACT_ALLOW" and not g.get("includes") and not g.get("excludes"):
        allow.update(g["names"])
print("default action:", out["defaultAction"])
print("hardening-denied (now ERRNO):", len(removed), sorted(removed))
print("unconditional-allow syscalls remaining:", len(allow))
for probe in ["clone","execve","openat","fork","epoll_wait","pipe2","ptrace","mount","bpf"]:
    print(f"  {probe:12} {'ALLOW' if probe in allow else '(cond/denied)'}")
