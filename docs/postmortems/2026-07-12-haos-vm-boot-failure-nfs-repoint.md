# Postmortem: Home Assistant VM went read-only and failed to boot after live NFS re-point

**Date:** 2026-07-12
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV2 (Home Assistant + Zigbee2MQTT unavailable during the
incident; no data loss)

## Summary
Re-pointing an actively-mounted NFS share to a new network path while a
VM was running against it caused the guest's root filesystem to go
read-only, and a subsequent reset failed to find a bootable device. A
clean stop/wait/start cycle (rather than another reset) resolved it with
no data loss.

## Impact
Home Assistant and Zigbee2MQTT unavailable for the duration of the
incident (well under an hour); no persistent data loss, confirmed via
`zpool status` before and after.

## Timeline
| Time | Event |
|---|---|
| T+0 | `tank-fast-nfs` re-pointed from its LAN address to the dedicated storage-network link using `umount -l` (lazy unmount) on the old mountpoint, while VM 100 (Home Assistant) was actively running against it |
| T+1m | Home Assistant console shows `Read-only file system` errors; Zigbee2MQTT logs freeze mid-stream |
| T+2m | `qm reset 100` attempted -- fails with `BdsDxe: No bootable option or device was found` |
| T+3m | `zpool status` checked -- pool healthy, zero errors. NFS mount on the Proxmox side checked -- healthy, correctly pointed at the new address |
| T+4m | `qm stop 100`, `sleep 10`, `qm start 100` -- VM boots cleanly |

## Root Cause
Lazy unmount (`umount -l`) detaches a mountpoint from the filesystem
tree immediately but only frees the underlying resource once the last
user releases it. In the window between the detach and the new mount
stabilizing, the running VM took I/O errors on its open files, and the
guest kernel defensively remounted its root filesystem read-only -- the
correct, data-safe behavior for a guest that suddenly can't trust its
storage. The subsequent `qm reset` then raced against the NFS mount not
yet being fully settled and briefly couldn't see the boot disk at all.

## Trigger
A live storage re-point (changing the NFS mount's network path) was
performed while the consuming VM was still running against the old
mount, using a lazy unmount instead of stopping the VM first.

## Detection
Manual -- noticed via the Home Assistant console showing filesystem
errors and Zigbee2MQTT logs stopping.

## Resolution
1. Verified `zpool status` (pool healthy) and the Proxmox-side NFS mount
   (healthy, correctly pointed) first, to rule out actual data loss
   before touching anything further
2. `qm stop 100` -- a full stop releases the QEMU process's file handle
   entirely, unlike `reset`
3. `sleep 10` -- gives NFS time to fully settle before boot is attempted
4. `qm start 100` -- VM came up cleanly

## Action Items
- [x] Rule documented: when re-pointing storage actively mounted by a
      running VM, stop the VM first, switch the storage backend, then
      start it -- never do it live with a lazy unmount
- [ ] Consider whether future storage-network changes can be scripted
      with an explicit VM-stop step built in, rather than relying on
      remembering the rule each time

## Lessons Learned
- A lazy unmount is not equivalent to a clean unmount from the
  perspective of an active consumer -- it defers resource release, not
  I/O interruption, and a running VM will still see the interruption
- When storage appears to fail catastrophically (unbootable VM), verify
  the storage layer itself (pool health, mount status) before assuming
  data loss -- in this case the guest's own defensive read-only
  remount was actually evidence the data was being protected, not lost
- `reset` and `stop`+`start` are not interchangeable recovery actions;
  `stop` guarantees full resource release in a way `reset` does not
