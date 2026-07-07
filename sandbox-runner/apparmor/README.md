# apparmor — `eca-agent` profile (ECA-64, AC#1) — **Linux/ECS path only**

AppArmor is the second MAC layer for the **Linux** deployment tier. It is shipped
here for completeness of the AC#1 control map, but:

> **On Docker Desktop macOS it is a NO-OP.** The LinuxKit VM ships no AppArmor
> LSM. Verified empirically: `--security-opt apparmor=<nonexistent>` exits `0`
> (a real AppArmor host errors "profile does not exist"). So on Mac peers the
> profile is silently ignored and **seccomp** (`../seccomp/eca-seccomp.json`) is
> the mandatory-access-control substitute. Do not claim AppArmor enforcement on
> Mac (container-sandbox.md residual).

## Install (Linux host only)

```bash
sudo apparmor_parser -r -W sandbox-runner/apparmor/eca-agent
docker run --security-opt apparmor=eca-agent ...
```

## What it adds

Derived from moby's `docker-default` template, plus a hardening delta:

- `deny network raw` / `deny network packet` — no raw sockets / sniffing.
- `deny ptrace` (except peer=`eca-agent`) — no cross-process debugging.
- `/proc` write hardening — no sysctl writes (except `kernel/shm*`), no
  `kcore`/`kmem`/`mem`/`sysrq-trigger`.
- `/sys` hardening — no firmware, no `securityfs`, no kernel writes.
- `deny mount` / `deny pivot_root` — belt with the seccomp mount-family denies.
- explicit `deny capability sys_admin|sys_module|sys_ptrace|sys_rawio|…`.

The broad `file,` / `network,` allows are inherited from `docker-default` on
purpose: the workload's file access is **not** path-restricted in this profile
(that would need per-path tuning validated on a real Linux host, which this Mac
peer cannot exercise), so hardening is expressed as explicit denies on top —
exactly the posture `docker-default` uses. `--cap-drop=ALL` + seccomp remain the
primary layers on every tier.
