# Postmortem: syncthing CIFS mount failure on k3s-burst-worker after VM resize

**Date:** 2026-09-09
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV3 (single app, no cross-service impact, workaround was to wait for fix)

## Summary
After downsizing the `k3s-burst-worker` Proxmox VM (4 vCPU/8GiB → 2 vCPU/4GiB,
a routine right-sizing change since the node was running at ~5% CPU / ~29%
memory), the `syncthing` pod — which is `nodeSelector`-pinned to this node —
came back `ContainerCreating` and stayed stuck: its SMB-backed PVC failed to
mount with `mount error(79): Can not access a needed shared library`. Root
cause was an unrelated, pre-existing gap: the node's kernel had been
upgraded by `apt` (unattended-upgrades) weeks earlier but never rebooted, so
`linux-modules-extra` for the new kernel — which provides the `nls_utf8`
module CIFS needs — was never installed. Our VM resize forced the first
reboot since that kernel upgrade, exposing the gap. Fixed by running the
existing Ansible `kernel-modules` role against the node, which installed the
missing package and loaded the module.

## Impact
`syncthing` (namespace `syncthing`) unavailable for ~15 minutes during the
maintenance window. No other service affected — this was the only pod on
the node with a hard `nodeSelector` to `k3s-burst-worker` combined with an
SMB-backed PVC; Prometheus/Grafana/Alertmanager use `local-path` and
remounted fine, everything else floated to other nodes during the drain.

## Timeline
All times UTC, 2026-09-09.

| Time | Event |
|------|-------|
| ~15:10 | Right-sizing decision made: `k3s-burst-worker` measured at 208m CPU (5%) / 2.3GiB (29%) against 4 vCPU/8GiB allocated |
| ~15:15 | `kubectl cordon` + `kubectl drain k3s-burst-worker --ignore-daemonsets --delete-emptydir-data` — no PDBs blocked it |
| ~15:20 | On Proxmox: `qm shutdown 101` → `qm set 101 --cores 2 --memory 4096` → `qm start 101` |
| ~15:30 | VM boot console showed repeated `CIFS: VFS: CIFS mount error: iocharset utf8 not found` during cloud-init — initially assumed to be unrelated boot noise |
| ~15:31 | Node rejoined as `Ready`; `kubectl uncordon k3s-burst-worker` |
| ~15:36 | Verified: Grafana/Prometheus (local-path) back `Running`; `syncthing` stuck `ContainerCreating`, `FailedMount` events referencing the same CIFS/iocharset error |
| ~15:38 | `kubectl describe pod` on syncthing confirmed `nodeSelector: kubernetes.io/hostname: k3s-burst-worker` — pod cannot reschedule elsewhere, must be fixed on this node |
| ~15:40 | SSH to node: confirmed `nls_utf8` module file absent for running kernel `6.8.0-139-generic` (`modinfo nls_utf8` → not found) |
| ~15:42 | Ran `ansible-playbook site.yml --limit k3s-burst-worker` — existing `kernel-modules` role detected the gap, installed `linux-modules-extra-6.8.0-139-generic`, loaded `nls_utf8`, persisted it via `/etc/modules-load.d/` |
| ~15:44 | `syncthing` pod remounted successfully and reported `1/1 Running` |

## Root Cause
`k3s-burst-worker`'s kernel had been upgraded to `6.8.0-139-generic` by
`apt-daily-upgrade.timer` at some point before this incident, but the VM was
never rebooted afterward, so it kept running on the older, already-loaded
kernel (134) where `nls_utf8` was present. `linux-modules-extra` for the new
kernel (139) was never installed — nothing triggers that automatically on
this distro; it's a separate package from the kernel image itself. When the
VM finally rebooted (as a side effect of the resize), it booted onto 139,
and `nls_utf8.ko` simply didn't exist on disk for that kernel version, so
any CIFS mount requiring UTF-8 charset conversion failed at the kernel
level — surfacing first as boot-time `dmesg` noise (a host-level fstab/CIFS
mount, not directly relevant), then as a hard failure for the CSI-driven
SMB mount `syncthing`'s PVC depends on.

## Trigger
VM reboot (triggered by an unrelated, routine CPU/RAM right-sizing change)
was the first reboot since a kernel upgrade that had gone un-rebooted for
an unknown but non-trivial period.

## Detection
Manual — noticed while verifying pods came back healthy after the planned
maintenance. There was no alert for this; `kubectl get pods -A` showed the
`ContainerCreating` state during routine post-change verification, not
because of a paging alert. Something to fix (see Action Items).

## Resolution
1. Confirmed via `kubectl describe pod` that `syncthing` could not
   reschedule off the node (`nodeSelector` pin), so the fix had to happen
   on `k3s-burst-worker` itself, not by moving the workload.
2. SSH'd to the node and confirmed the missing kernel module directly
   (`modinfo nls_utf8`), ruling out a CSI-driver-side or userspace
   `cifs-utils` problem (a red herring initially suspected, since the SMB
   CSI driver bundles its own `mount.cifs` inside its container image and
   doesn't depend on the host having `cifs-utils` installed at all).
3. Used the existing, previously-written Ansible role
   (`ansible/roles/kernel-modules/`) — already covered `k3s-burst-worker`
   in the inventory — instead of hand-fixing the node, keeping the fix
   consistent with how every other node in the fleet gets this dependency
   managed.
4. Verified the pod remounted and came up healthy.

## Action Items
- [ ] Run the Ansible `kernel-modules` (and `k3s-file-permissions`) role
      automatically and regularly across the fleet, so a kernel-upgrade +
      reboot drift like this is caught before it causes a stuck pod, not
      discovered by accident during unrelated maintenance. (In progress —
      see follow-up work same day.)
- [ ] Consider whether `linux-modules-extra-generic` (the unversioned
      tracking meta-package, as opposed to the version-pinned package the
      role currently installs reactively) should be installed proactively
      on all Ubuntu nodes, so `apt upgrade` pulls matching extra modules in
      the same transaction as any future kernel upgrade — closing this
      entire class of drift structurally rather than detecting it after
      the fact.
- [ ] No alerting currently exists for `ContainerCreating`/`FailedMount`
      stuck pods cluster-wide; cross-reference with the `CrashLoopBackOff`
      alerting gap noted in the 2026-09-08 training-drill postmortem
      (`SRE/postmortems/`) — likely the same Prometheus rule work covers
      both.

## Lessons Learned
- A "safe, reversible" change (right-sizing a VM that's using 5% CPU) can
  still surface unrelated latent problems, because reboot is not a no-op —
  it's the first time in a long while that boot-time init actually re-runs
  from scratch. Don't assume a low-risk change has no blast radius just
  because the change itself is small; the *reboot* is what carries risk on
  a long-uptime node, independent of what you actually changed.
- `nodeSelector`-pinned pods (no alternative node to fail over to) deserve
  extra attention during any maintenance on their pinned node — check for
  these before draining, not just after something gets stuck.
- Don't trust the first plausible explanation under a symptom that matches
  a red herring: the boot-console CIFS errors looked identical to the
  actual failure and were initially waved off as "probably unrelated
  noise" — they were the same root cause, just surfacing twice.
- Infrastructure this repo already has Ansible roles for (like
  `kernel-modules`) is only as good as how often it actually runs. A
  correct, idempotent role sitting unused between manual invocations is a
  detection gap, not a fix.
