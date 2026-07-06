# seccomp — `eca-seccomp.json` (ECA-64, AC#1)

The syscall MAC (mandatory access control) layer for the agent sandbox. On
Docker Desktop **macOS** this is the *substitute* for AppArmor (which the LinuxKit
VM silently ignores — see `../apparmor/README.md`); on Linux/ECS it stacks with
the AppArmor profile.

The spawner (ECA-65) passes it at launch:

```
docker run --security-opt seccomp=/path/to/seccomp/eca-seccomp.json ...
```

## What it is

`eca-seccomp.json` is the upstream **moby default** seccomp profile
(`defaultAction: SCMP_ACT_ERRNO`, i.e. default-deny) with an extra **hardening
delta** applied: a set of syscalls the `python + git + Claude Code CLI` workload
never needs is stripped so they hard-`ERRNO` **regardless of capabilities**.

Basing on the moby default (not a hand-rolled allowlist) is deliberate: it is the
battle-tested set that already runs arbitrary `git`/`python` workloads, so the
353 allowed syscalls are exactly what the runner needs — no runtime breakage — and
the tightening is a small, auditable *subtraction* on top.

### Hardening delta (26 syscalls forced to ERRNO beyond `--cap-drop=ALL`)

| Family | syscalls |
|---|---|
| debug / cross-process | `ptrace`, `process_vm_readv`, `process_vm_writev`, `kcmp` |
| persona / ASLR | `personality` |
| mount / fs-namespace | `mount`, `umount2`, `pivot_root`, `chroot`, `mount_setattr`, `move_mount`, `open_tree`, `fsopen`, `fsconfig`, `fsmount`, `fspick` |
| namespace create/join | `unshare`, `setns` |
| kernel keyring | `keyctl`, `add_key`, `request_key` |
| bpf / perf / io-ports / accounting / modules | `bpf`, `perf_event_open`, `ioperm`, `iopl`, `acct`, `quotactl`, `nfsservctl`, `swapon`, `swapoff`, `kexec_load`, `kexec_file_load`, `init_module`, `finit_module`, `delete_module` |

Most of these are already cap-gated in the base (denied once `--cap-drop=ALL`
lands), but forcing them to `ERRNO` unconditionally is defense-in-depth: it holds
even if a future launch mistakenly adds a capability back.

**`clone`/`clone3` are intentionally NOT in the delta** — `fork()` and
`pthread_create()` need `clone`. The base's *conditional* `clone` group (which
excludes the namespace-creating flags when `CAP_SYS_ADMIN` is absent) is
preserved verbatim, so normal fork works but `clone(CLONE_NEW*)` still fails.

## Reproducing / updating

`eca-seccomp.json` is generated, not hand-edited (seccomp JSON has no comments):

```
# base = the pinned upstream default (kept alongside for auditable diffing)
curl -fsSL -o moby-default-base.json \
  https://raw.githubusercontent.com/moby/moby/v27.5.1/profiles/seccomp/default.json
python3 generate.py     # applies TIGHTEN_DENY -> eca-seccomp.json
```

To re-verify the profile is valid and does not break the workload, run the smoke
(`../smoke/smoke.sh`) — it launches the agent container *with* this profile and
performs a real `git clone` + `python -c "import ..."`; a missing syscall would
fail the boot/clone leg loudly.

## macOS caveat

Docker Desktop enforces seccomp inside the LinuxKit VM (`Security Options` lists
`seccomp`), so this profile **is** applied on Mac peers — unlike AppArmor. It is
the primary MAC layer on the operator-Mac tier.
