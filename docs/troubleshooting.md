# Troubleshooting Log

> A chronological log of real incidents hit while building and operating
> this homelab, and how they were diagnosed and fixed. Kept separate from
> [architecture.md](architecture.md) on purpose: architecture describes the
> stable, current shape of the system; this document is a running history
> of what went wrong along the way and the diagnostic process used to get
> to a fix. See architecture.md for what the system looks like today --
> come here for why some of it works the way it does, and for patterns
> worth remembering next time something breaks.

## 2026-07-11 -- initial TrueNAS/Proxmox integration

- **Proxmox had no working DNS** (`/etc/resolv.conf` pointed at an
  unrelated `192.168.1.1`), blocking `wget` of the HAOS image -- fixed by
  pointing at a working resolver.
- **ASUS SMB shares are one-share-per-folder**, not one share with
  subfolders -- `mount -t cifs //router/New_Volume` doesn't exist as such;
  discovered via `smbclient -L`.
- **CIFS mount `error(13)` Permission denied** traced to the SMB user
  lacking per-folder share permissions on the router side (not a
  mount-options problem).
- **Jellyfin/FileBrowser stuck in `ContainerCreating`** after an unrelated
  physical disk swap severed the old `nas-smb` mount -- resolved by
  provisioning new SMB-backed PVCs and patching the Deployments' volume
  claims, rather than trying to resurrect the dead mount.
- **Zigbee2MQTT onboarding form silently reset** the adapter/port
  selection on page reload, causing `zigbee-herdsman` to fail adapter
  auto-discovery -- worked around by setting the adapter/port explicitly
  via the onboarding wizard's dedicated "Serial" tab instead of the
  auto-populated main tab.
- **`ember` adapter failed to initialize** the Zigbee radio (`ASH`
  handshake reset loop, `HOST_FATAL_ERROR`) despite the dongle's
  multiprotocol marketing -- `zstack` worked on the first try.
- **Jellyfin playback failure on one file** traced to the file living in
  the deliberately-unmigrated `torrents` folder, not a storage or codec
  issue; confirmed healthy by testing playback from the migrated library
  instead.

## 2026-07-12 -- follow-up incidents

Three separate issues surfaced the day after the initial migration --
documented in detail because each one taught a reusable lesson about the
new architecture, not just a one-off fix.

### HAOS VM boot failure after live NFS re-pointing

**Symptom:** Home Assistant's filesystem silently went read-only
(`Read-only file system` errors in the console, Zigbee2MQTT logs frozen
mid-stream), and a subsequent `qm reset 100` failed outright with
`BdsDxe: No bootable option or device was found.`

**Root cause:** re-pointing `tank-fast-nfs` from its LAN address to the
dedicated storage-network link (see architecture.md §5) used `umount -l`
(lazy unmount) on the old mountpoint while VM 100 was actively running
against it. Lazy unmount detaches the mountpoint from the filesystem tree
immediately but only frees the underlying resource once the last user
releases it -- in the window between that detach and the new mount
stabilizing, the running VM took I/O errors on open files and the guest
kernel defensively remounted its root filesystem read-only (the correct,
data-safe behavior for a guest that suddenly can't trust its storage).
The subsequent `qm reset` then raced against the NFS mount not yet being
fully settled and briefly couldn't see the boot disk at all.

**Fix:** verified `zpool status` (pool healthy, zero errors) and the NFS
mount on the Proxmox side (healthy, correctly pointed at the new address)
first, to rule out actual data loss. Then did a clean `qm stop 100` /
`sleep 10` / `qm start 100` instead of another `reset` -- a full stop
releases the QEMU process's file handle entirely and the sleep gives NFS
time to fully settle before boot is attempted again. VM came up cleanly;
no data was lost.

**Lesson for next time:** when re-pointing storage that's actively
mounted by a running VM, stop the VM first, switch the storage backend,
then start it -- rather than doing it live with a lazy unmount. It
worked out this time, but the safer sequence avoids the guest ever seeing
a storage interruption at all.

### qBittorrent stuck in `ContainerCreating` (same root cause as Jellyfin, different day)

**Symptom:** `qbittorrent` pod pending for hours, `FailedMount` events
citing `could not resolve address for ASUS`.

**Root cause:** identical to the Jellyfin/FileBrowser incident from
2026-07-11 above -- the physical disk swap on the ASUS router had already
made `nas-smb` permanently unreachable, and qBittorrent's `config` and
`downloads` PVCs were still pointing at it.

**Fix (same-night, temporary):** since qBittorrent and its Firefox/noVNC
sidecar are both pinned to `g3-worker3` via `nodeSelector` and never
migrate between nodes, `local-path` was used as an immediate replacement
for `config` and `downloads` -- no TrueNAS share needed for a same-node
PVC. `downloads` was later moved to a proper TrueNAS-backed share
(`tank-bulk/downloads`, see architecture.md §3.2/§4.2) once there was
time to do it properly; `config` stayed on `local-path` since it's small,
node-pinned, and doesn't need network access from elsewhere.

### FileBrowser PVC stuck in `Pending` forever: `nodeName` bypasses the scheduler

**Symptom:** a `local-path` PVC for FileBrowser's config never bound.
`kubectl describe pvc` showed only `WaitForFirstConsumer`, repeating
indefinitely, with no `Provisioning` event ever appearing in the
`local-path-provisioner` logs -- even though the *exact same StorageClass*
had just successfully provisioned a volume for qBittorrent minutes
earlier. Restarting the provisioner pod, and deleting/recreating the PVC
several times, made no difference.

**Root cause:** FileBrowser's Deployment used `spec.nodeName: g3-worker3`
directly, while every other Deployment in the cluster uses
`spec.nodeSelector`. `nodeName` bypasses the Kubernetes scheduler
entirely -- the kubelet on the named node picks the pod up directly. But
it's the *scheduler* that's responsible for annotating a
`WaitForFirstConsumer` PVC with `volume.kubernetes.io/selected-node` once
it places a pod that needs it; since the scheduler was never involved,
that annotation never got written, and `local-path-provisioner` (which
watches for that annotation) had nothing to act on. The pod otherwise
looked completely normal (`PodScheduled: True`, correct node in
`describe pod`), which made this misleading to diagnose -- it looks
identical to a provisioner problem until you check `Node-Selectors` in
the pod description and notice it's empty.

**Fix:** changed FileBrowser's Deployment from `nodeName: g3-worker3` to
`nodeSelector: {kubernetes.io/hostname: g3-worker3}` (functionally
equivalent pod placement, but goes through the scheduler). The very next
PVC created bound immediately.

**Lesson for next time:** `nodeName` and `nodeSelector` are not
interchangeable even though they often produce the same placement result --
`nodeName` skips scheduling-time logic entirely, which breaks anything
that depends on the scheduler doing work at bind time (like
`WaitForFirstConsumer` volume binding). Audited the rest of the repo
afterwards (`grep -rn nodeName k8s/`) and confirmed FileBrowser was the
only Deployment using it.
