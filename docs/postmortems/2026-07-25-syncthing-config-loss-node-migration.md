# Postmortem: Syncthing configuration destroyed during a routine node migration

**Date:** 2026-07-25
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV2 (service configuration lost -- device pairings, folder
definitions; underlying synced data was never at risk)

## Summary
Migrating Syncthing from a Raspberry Pi to a Proxmox VM for CPU headroom
triggered an infinite crash loop (`SQLITE_BUSY`) because its SQLite
database, which requires real POSIX file locking, ended up briefly on an
SMB-backed volume that doesn't reliably support that. During cleanup,
the original config PVC was deleted along with its PV under a `Delete`
reclaim policy, permanently destroying the Syncthing configuration
(Device ID, all peer pairings, folder definitions) -- though the actual
synced files, on a separate PVC, were untouched throughout.

## Impact
Syncthing configuration (not data) lost entirely; every peer device had
to be manually re-paired (new Device ID). Forced a from-scratch full
folder rescan on next startup, which is what surfaced a separate,
larger-scale mojibake filename problem the same day (see the
`nls_utf8` and mojibake-duplicate-files postmortems). No synced files
were lost.

## Timeline
| Time | Event |
|---|---|
| T+0 | Syncthing's `nodeSelector` re-pointed from `pi4-worker2` to `k3s-burst-worker` (a Proxmox VM) to relieve CPU pressure from large periodic folder scans |
| T+2m | New pod pending -- `local-path` PVCs are pinned to the node they were first provisioned on via `nodeAffinity` baked into the PV; they don't follow a `nodeSelector` change on the Deployment. New pod unschedulable against the old config volume |
| T+10m | Attempted fix: moved the config PVC onto the `smb-tank-bulk-syncthing` StorageClass instead, assuming any RWX-capable storage would work |
| T+12m | Pod enters an infinite crash loop: `ERR Error opening database (error="openbase (PRAGMA journal_mode = WAL): database is locked (5) (SQLITE_BUSY)")`, restarting every second |
| T+20m | Root cause identified: Syncthing 2.x stores its index in SQLite with `journal_mode=WAL`, which requires real POSIX file locking that CIFS/SMB doesn't reliably provide |
| T+25m | During cleanup of the failed SMB-backed config volume, the *original* `local-path` PVC was also deleted -- its PV had `reclaimPolicy: Delete` (unlike the `Retain` policy on SMB-backed classes), so the underlying config data was destroyed with no way back |
| T+30m | Provisioned a fresh `local-path` PVC (`syncthing-config-local`) targeting `k3s-burst-worker` directly, and pointed the Deployment's config volume at it |
| T+35m | Syncthing started cleanly with a blank configuration; began re-pairing every peer device and re-adding folders at their existing paths |

## Root Cause
Two compounding storage mistakes:
1. `local-path` PVCs are node-pinned via `nodeAffinity` on the PV --
   they do not migrate when a Deployment's `nodeSelector` changes.
   Migrating the Deployment without first migrating the underlying
   storage left the new pod unschedulable
2. Attempting to fix that by moving the config onto an SMB-backed
   StorageClass broke Syncthing outright, since SQLite's WAL journal
   mode requires POSIX locking semantics CIFS doesn't reliably provide
3. The compounding failure: cleaning up the failed SMB attempt also
   deleted the original, still-good `local-path` PVC, whose PV had a
   `Delete` reclaim policy (unlike the `Retain` policy used on
   SMB-backed classes) -- destroying the configuration permanently

## Trigger
A routine infrastructure improvement (moving a CPU-heavy workload to a
more capable node) executed without first checking whether the
workload's storage class was compatible with node migration or with
the underlying database engine's locking requirements.

## Detection
Immediate and unambiguous -- the pod crash-looped visibly within
seconds of the storage class change.

## Resolution
1. Provisioned a fresh `local-path` PVC already targeting the new node
2. Pointed the Deployment's config volume at it instead of the SMB
   class
3. Accepted the configuration loss as unrecoverable (no backup existed)
   and re-paired every peer device from scratch
4. Established a standing rule: Syncthing's (or any SQLite-WAL-backed
   app's) config/database must live on `local-path` or another volume
   with real POSIX locking -- never on `smb-tank-*`. Only the *synced
   data itself* is safe on SMB

## Action Items
- [x] Rule documented and applied consistently to subsequent
      SQLite-backed deployments in this cluster (e.g. the `arr` stack
      apps, Prowlarr/Sonarr/Radarr, and the MinIO/Terraform state work)
- [ ] Consider whether Syncthing's config directory is worth a periodic
      backup (e.g. a CronJob copying `/config` to the SMB-backed data
      volume) given how costly a repeat of this would be, since there
      is currently no recovery path if `local-path` data is lost
- [x] When migrating a `local-path`-backed workload to a new node in
      future, provision the new node's PVC *first*, verify it schedules
      and mounts cleanly, and only then decide whether the old PVC/PV is
      safe to delete -- never delete-then-recreate under time pressure

## Lessons Learned
- Losing configuration is not the same severity as losing data --
  Syncthing re-indexes existing files by hash rather than
  re-downloading them once a folder is re-added at the same path, so
  the actual synced files were never at risk. But it *did* cost real,
  non-automatable re-pairing work with every peer device, and it forced
  a full rescan that surfaced an unrelated, larger problem
- CIFS/SMB is not a general-purpose "any RWX storage will do" solution
  -- specific applications with locking requirements (SQLite WAL being
  the most common) need POSIX-compliant storage regardless of whether
  SMB technically supports the access mode requested
- `local-path`'s `Delete` reclaim policy is a sharp edge during cleanup
  operations -- when troubleshooting under time pressure and deleting
  PVCs to "start clean," it's easy to delete something still-needed
  alongside the actual broken resource. Deleting one specific,
  double-checked resource at a time is safer than a broad cleanup pass
