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
## 2026-07-25 -- qBittorrent port drift, Syncthing node migration, CIFS mojibake and stale dentry cache

A single day covering three unrelated incidents, kept together because two
of them (Syncthing config loss, stale CIFS cache) were only discovered
*because of* migrating Syncthing off `pi4-worker2` for CPU headroom -- each
fix exposed the next problem.

- **qBittorrent: dozens of peers, 0 B/s transfer, healthy WireGuard
  handshake.** A healthy tunnel only proves outbound connectivity --
  ProtonVPN's NAT-PMP-forwarded port had rotated after a tunnel restart,
  and qBittorrent's `Listening Port` setting doesn't auto-follow it (UPnP/
  NAT-PMP was off in Options -> Connection). Peers could see the swarm
  entry but not complete an inbound connection on the now-stale port.
  Fixed by manually updating `Listening Port` to the currently-forwarded
  one; transfer resumed after ~15-30 min once tracker/DHT/PEX propagated
  it. A `natpmpc`-based systemd timer that re-checks the forwarded port
  and pushes it to qBittorrent's WebUI API on change is designed but not
  yet deployed -- see `setup/05-qbittorrent-vpn.md`.

### Editing a config file on a volume a live process owns races the process itself

**Symptom:** a `sed`-edited setting in `qBittorrent.conf`
(`WebUI\ServerDomains`) verified correct immediately after editing, but
reverted after `kubectl rollout restart` -- repeatedly, across three
separate attempts.

**Root cause:** the running process rewrites its own config file from
memory on shutdown. Editing the file on disk while the process is still
alive is a race: whichever write happens last wins, and a graceful
`SIGTERM` shutdown reliably loses that race in favor of the process's
stale in-memory state.

Compounded by a second problem discovered along the way: this cluster had
accumulated **three different PVCs all plausibly named for qBittorrent's
config** across earlier migrations (`qbittorrent-config` on
`local-path`/pi4-worker2, `qbittorrent-config-nas` on SMB,
`qbittorrent-config-local` on `local-path`/g3-worker3). Editing the wrong
one wasted significant time -- `kubectl get deployment ... -o yaml`'s
`last-applied-configuration` annotation reflected an older manifest, not
the live one, and pointed at a PVC the Deployment no longer actually used.

**Fix:**
- To find the volume actually mounted, don't trust the
  `last-applied-configuration` annotation -- read the live spec and cross
  check with what's mounted inside the pod:
  ```bash
  kubectl get deployment <name> -n <ns> -o jsonpath='{.spec.template.spec.volumes}' | jq
  kubectl exec -n <ns> deployment/<name> -- df -h /config
  ```
  `df -h` distinguishes a local block device (`/dev/sdX`) from a network
  mount (reports as `//ip/share`) immediately.
- Never edit a config file belonging to a process expected to rewrite it
  on exit while that process is still running. Scale to 0, wait for the
  pod to be **fully gone** (`kubectl get pods -w` shows nothing -- not
  just `Terminating`, which can sit for 30s+ under the default grace
  period), edit on disk, then scale back to 1.
- Housekeeping debt from this: the two orphaned qBittorrent config PVCs
  are still sitting in the cluster unreferenced by anything live. Worth an
  audit pass (`kubectl get pvc -A`) at some point.

### Syncthing config destroyed migrating pi4-worker2 -> Proxmox VM (SMB doesn't support SQLite WAL locking)

**Symptom:** after re-pointing Syncthing's `nodeSelector` to
`k3s-burst-worker` (a Proxmox VM, moved there because Syncthing's periodic
full-tree scans of large `venv`/`.vs`/`node_modules` directories were
pegging the Pi's CPU -- visible as a correlated ~15W jump on the TrueNAS
power monitor from the resulting SMB client load), the pod entered an
infinite crash loop: `ERR Error opening database (error="openbase
(PRAGMA journal_mode = WAL): database is locked (5) (SQLITE_BUSY)")`,
restarting every second.

**Root cause:** two compounding storage mistakes, both learned the hard
way rather than checked in advance:
1. `local-path` PVCs are pinned to the node they were first provisioned on
   via `nodeAffinity` baked into the PV -- they don't follow a
   `nodeSelector` change on the Deployment. Migrating the Deployment
   without migrating the underlying storage class first left the new pod
   unschedulable.
2. Attempting to fix that by moving the config PVC onto the
   `smb-tank-bulk-syncthing` StorageClass broke Syncthing outright.
   Syncthing 2.x stores its index in SQLite with `journal_mode=WAL`, which
   requires real POSIX file locking -- CIFS/SMB doesn't reliably provide
   this, hence the permanent `SQLITE_BUSY`.
3. During cleanup, the original `local-path` PVC was deleted along with
   its PV (`reclaimPolicy: Delete` on `local-path`, unlike the `Retain`
   policy on the SMB-backed classes -- see architecture.md §4.2), which
   destroyed the config -- Device ID, all peer pairings, folder
   definitions -- with no way back.

**Fix:** provisioned a fresh `local-path` PVC
(`syncthing-config-local`) already targeting `k3s-burst-worker`, and
pointed the Deployment's `config` volume at it instead of the SMB class.
**Rule going forward: Syncthing's (or any SQLite-WAL-backed app's) config/
database must live on `local-path` or another volume with real POSIX
locking -- never on `smb-tank-*`.** Only the synced *data* itself is safe
on SMB.

**Lesson for next time:** losing the config wasn't data loss for the
synced files (`syncthing-data` on SMB was untouched throughout) -- Syncthing
re-indexes existing files by hash rather than re-downloading them once a
folder is re-added at the same path. It *did* cost real re-pairing work
with every peer device (new Device ID), and -- more consequentially -- it
forced a from-scratch full rescan of both folders, which is what surfaced
the mojibake filename problem below at full scale instead of gradually.
When migrating a `local-path`-backed workload to a new node in future,
provision the new node's PVC *first*, verify it schedules and mounts
cleanly, and only then decide whether the old PVC/PV is safe to delete --
don't delete-then-recreate under time pressure.

### Double-encoded (mojibake) Polish filenames on the TrueNAS SMB shares

**Symptom:** Syncthing's from-scratch rescan (triggered by the config loss
above) surfaced `scan: item is not in UTF8 encoding` and `hashing: open
...: no such file or directory` for files with Polish diacritics (ą, ę, ż,
ń, ś, ó) in the name, across two shares (`Projects` and `Syncthing`).
`ls` displays the broken names with a literal `?` where the letter should
be.

**Root cause:** at some point files were copied through a tool that
mis-interpreted already-valid UTF-8 bytes as Windows-1252/Latin-1 and
re-encoded them -- a double-encoding bug. This leaves a second, garbled
*duplicate* of the filename sitting next to the correctly-encoded original
(byte-identical content, different name). `convmv -f windows-1250 -t utf8`
reports these as "already UTF-8" and does nothing, because they technically
are valid UTF-8 -- just the wrong UTF-8 -- so this isn't a `convmv`-fixable
encoding mismatch, it's stray duplicate files to find and remove.

**Diagnosis method:**
```bash
# see real bytes instead of terminal-mangled "?" placeholders
find "<path>" -type f | cat -v
# spot pairs by length -- the mojibake name is always longer than the healthy one
find "<path>" -type f -printf '%f\n' | awk '{ print length, $0 }' | sort -n
```
Before deleting anything, confirm the pair is byte-identical using shell
globs or array expansion so the shell resolves the actual bytes --
**never hand-type or copy/paste the broken filename into a command**,
both mangle multi-byte sequences further:
```bash
files=(Strona*Biznesowy.html)   # shell resolves real bytes via glob
diff -q "${files[0]}" "${files[1]}"
```

**Fix applied** (scoped to the `Projects` folder, ~64 files across
`Bujalski/E/db/W toku/` and `AnalitykBiznesowy_AI_Team_v2/gsc-exports/`):
for every healthy filename, `diff -q` it against every longer candidate
name in the same directory; only delete a candidate that diffed
byte-identical. `rm`, not rename -- a valid healthy copy already existed
in every case found.

**Still open:** the same pattern exists at much larger scale in the
second Syncthing folder (`02_Dev`, `03_Osobiste`, `01_Praca` --
hundreds of files, mostly old Visual Studio build artifacts and personal
documents). These currently sit as non-blocking `WRN Failed to scan`
warnings -- the rest of each folder syncs fine around them. For the VS
build-artifact trees specifically (`.vs/`, `bin/`, `obj/`, `.suo`), adding
them to Syncthing's ignore patterns is probably the better fix over
repairing their names, since they're disposable build output anyway.

### CIFS directory-listing cache survives pod restart and `drop_caches`, needs a forced remount

**Symptom:** after deleting the mojibake duplicates above directly on the
TrueNAS share from a different client, Syncthing on `k3s-burst-worker`
kept re-reporting the exact same "already fixed" files as errors --
surviving a full `db/scan` API call, a pod restart via `kubectl rollout
restart`, and `echo 3 > /proc/sys/vm/drop_caches` run inside a `kubectl
debug node/... --image=busybox -- chroot /host bash` session. `errors`
count via the Syncthing REST API stayed at a stubborn 66 through all of
it, with `state: idle` -- meaning Syncthing genuinely believed a fresh
scan still saw these files.

**Root cause:** the CSI SMB mount's `actimeo=1` option only bounds
*attribute* cache TTL (file size, mtime), not the CIFS **directory entry
cache** under `cache=strict` mode. This cache is scoped to the **node's
kernel**, not the pod -- the mount is a `globalmount` that a new pod
schedules onto and reuses as-is, so a pod restart never touches it. And
`chroot /host` inside a `kubectl debug` session changes the *visible root
filesystem* but not the *mount namespace* -- writing
`/proc/sys/vm/drop_caches` there doesn't reliably reach the host's actual
page/dentry cache, which is why that attempt did nothing.

**Fix:** force-unmount the stale `globalmount` and let the CSI driver
recreate it clean on next schedule.
```bash
# 1. scale to 0 so nothing holds the mount open
kubectl scale deployment <name> -n <ns> --replicas=0
kubectl get pods -n <ns> -w    # wait for full removal

# 2. get a shell actually inside the host's namespaces -- plain chroot
#    is not enough for unmount; need nsenter into PID 1's namespaces,
#    which needs a privileged debug profile
kubectl debug node/<node> -it --image=busybox --profile=sysadmin \
  -- nsenter -t 1 -m -u -i -n -p -- bash

# 3. force-unmount the relevant globalmount(s)
mount | grep <share-name>
umount -f /var/lib/kubelet/plugins/kubernetes.io/csi/smb.csi.k8s.io/<hash>/globalmount

# 4. scale back up
kubectl scale deployment <name> -n <ns> --replicas=1
```
Confirmed: `errors` went from 66 (surviving everything else tried) to
**0** immediately after the forced unmount/remount cycle.

**Lesson for next time:** `kubectl debug node/<x> -it --image=busybox --
chroot /host bash` is enough for read-only host filesystem inspection
only. Anything that needs real host privileges -- unmounting, `nsenter`
into other namespaces, `drop_caches` that actually takes effect -- needs
`--profile=sysadmin` (or a hand-written pod manifest with `privileged:
true`), otherwise the command *appears* to succeed (`umount` on a
`chroot`-only shell fails loudly with "must be superuser", which at least
makes the gap obvious -- `drop_caches` silently no-ops, which doesn't).
Also worth remembering for any future CIFS-backed PV: if a file
deleted/changed by a different client isn't showing up after a reasonable
propagation window, suspect the dentry cache before suspecting the
application.
## 2026-07-26 -- Forced unmount from a previous incident left a *different* PV's CIFS mount dead

**Symptom:** the `Syncthing` folder (separate from `Projects`, same Syncthing
instance) suddenly showed `Niezsynchronizowane` / `permission denied` on
`stat /data/Syncthing`, and `kubectl exec ... -- ls -la /data/` returned an
almost-empty directory owned by `root:root` instead of the expected ~78 GiB
tree. `df -h /data` from inside the pod reported `/dev/sda1` -- a local
block device -- instead of the `//192.168.50.21/syncthing` CIFS mount.

**Root cause:** yesterday's fix for the stale-dentry-cache incident
(2026-07-25 entry above) force-unmounted a specific `globalmount` on
`k3s-burst-worker` to clear a cache problem affecting the `Projects`
folder. That was the right fix for *that* PV, but `syncthing-data` (the
PVC backing the `Syncthing` folder) uses the **same node, same CIFS
share, same StorageClass** (`smb-tank-bulk-syncthing`) via a *separate*
`globalmount`. `mount | grep syncthing` on the node the next day confirmed
it: zero CIFS mounts present at all for that share, on either PV. Best
explanation: the node's `kubelet` didn't notice the PV's bind-mount had
gone stale until something (the day boundary, a routine scan, or simply
time) triggered a re-check -- at which point, with no live `globalmount`
underneath it to bind to, the pod's `/data` silently fell through to the
node's local filesystem instead of failing loudly.

**Fix:** same pattern as yesterday, but this time no manual `umount -f`
was even needed -- since nothing was mounted to begin with:
```bash
kubectl scale deployment syncthing -n syncthing --replicas=0
kubectl get pods -n syncthing -w   # wait for full removal
kubectl scale deployment syncthing -n syncthing --replicas=1
kubectl exec -n syncthing deployment/syncthing -- df -h /data
# -> //192.168.50.21/syncthing   11T   70G   11T   1%   /data
```
A clean scale-down/scale-up was sufficient to force the CSI driver to
establish a fresh `globalmount` from scratch.

**Side effect noticed while diagnosing:** `/data`'s root also contained
Syncthing *config* files (`cert.pem`, `config.xml`, `config.xml.v0`,
`key.pem`, `syncthing.lock`, `index-v2/`) left over from the brief period
(2026-07-25) when `syncthing-config-smb` pointed at this same share --
`smb-tank-bulk-syncthing`'s StorageClass doesn't carve out a distinct
`subDir` per PVC, so two PVCs against it can silently share the same root
directory. Harmless since that config PVC is no longer referenced by the
Deployment, but cleaned up (`rm` the stray files) to avoid confusion on a
future `ls /data`.

**Lesson for next time:** a force-unmount fix for a CIFS caching problem
is scoped to *the specific `globalmount` path*, not to the share as a
whole -- any other PV pointed at the same `source` share, even from the
same node, needs to be checked (and likely bounced) too, since it's an
independent `globalmount` under the hood. After any forced CIFS
unmount/remount, run `mount | grep <share>` on the node and cross-check
against every PV using that StorageClass (`kubectl get pv -o
jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.csi.volumeAttributes.source}{"\n"}{end}'`
| grep <share>`) before considering the incident closed.
## 2026-07-26 (cont.) -- Known issue: CIFS mount can't correctly read Polish diacritics in filenames (root cause found, fix not yet deployed)

**Symptom:** ~66-782 files with Polish diacritics in their names (ą, ę, ż,
ń, ś, ó, ł) consistently show up as Syncthing scan errors
(`hashing: open ...: no such file or directory`, `scan: item is not in
UTF8 encoding`) even though:
- the files are 100% correctly named on disk (verified with a raw hex
  dump straight off ZFS via the TrueNAS Shell, bypassing Samba entirely --
  see below)
- a completely independent CIFS mount from a different client
  (`pi4-master`, manual `mount -t cifs`) reads the same files correctly
- the PV backing the mount was fully deleted and recreated from scratch,
  ruling out any client-side or CSI-level cache
- SMB protocol version was bumped from 2.0 to 3.0, with zero change in
  behavior

**Root cause, confirmed step by step:**

1. Hex-dumped the filename bytes as seen by the Syncthing pod:
   ```bash
   kubectl exec -n syncthing deployment/syncthing -- sh -c \
     'find ".../Statystyki/" -maxdepth 1 -type f -printf "%f\n" | od -An -tx1'
   ```
   Result: `55 72 7a 3f 64 7a 65 6e 69 61` -- a literal ASCII `3f` (`?`)
   where `ą` should be.

2. Hex-dumped the *same* filename directly on TrueNAS via the built-in
   Shell (System Settings -> Shell in the WebUI, since SSH login was
   disabled), bypassing Samba/CIFS entirely:
   ```bash
   find /mnt/tank-bulk/syncthing/Projects/.../Statystyki/ \
     -maxdepth 1 -type f -printf "%f\n" | od -An -tx1
   ```
   Result: `55 72 7a c4 85 64 7a 65 6e 69 61` -- correct UTF-8 (`c4 85` =
   `ą`). **Confirmed: the data on ZFS is 100% correct.** The corruption
   happens exclusively in the CIFS layer between server and this specific
   client.

3. Checked the live `mount` options on the node
   (`kubectl debug node/... --profile=sysadmin -- nsenter -t 1 -m -u -i
   -n -p -- bash`, then `mount | grep syncthing`): no `iocharset` option
   was present at all -- the CIFS kernel module was silently falling back
   to some default charset that doesn't correctly decode multi-byte UTF-8
   from this particular Samba server/version combination.

4. Attempted to fix it by explicitly adding `iocharset=utf8` to the PV's
   `mountOptions` (StorageClass `mountOptions` are immutable after
   creation -- had to delete and recreate the PV itself, preserving
   `claimRef` so the existing PVC re-binds automatically without data
   loss, since `reclaimPolicy: Retain`). Result: **mount failed outright**:
   ```
   mount error(79): Can not access a needed shared library
   ```
   Root cause of *that*: `find /lib/modules/$(uname -r) -iname
   "nls_utf8*"` on `k3s-burst-worker` (Ubuntu 24.04.4, kernel
   `6.8.0-134-generic`) returns nothing -- the `nls_utf8` kernel module
   genuinely isn't present on this host, only `nls_iso8859_1` and
   `nls_ucs2_utils` are loaded. Requesting `iocharset=utf8` explicitly
   makes `mount.cifs` try to load a module that doesn't exist, and it
   fails loudly instead of silently falling back like it does when the
   option is omitted.

**Current state (workaround, not a fix):** reverted to `vers=3.0` without
an explicit `iocharset` -- this mounts successfully and is no worse than
before (same silent-fallback behavior that produces the `?` corruption
for diacritics, but at least the mount works and everything else
syncs normally). PV was deleted and recreated twice today; final working
manifest saved at `/tmp/pv-syncthing-data-new.yaml` on the dev container
(not yet committed to the repo -- do that before relying on it, since
`/tmp` doesn't survive a container rebuild).

**Where to pick this up next time:**
- The real fix is getting a working `nls_utf8`-equivalent available to
  the CIFS mount on `k3s-burst-worker`. Options to investigate, roughly
  in order of how invasive they are:
  1. Check whether `nls_utf8` ships in a separate package
     (`linux-modules-extra-6.8.0-134-generic` or similar) that isn't
     installed -- `apt list --installed | grep linux-modules` only shows
     the base `linux-modules` packages, not `-extra`.
  2. If truly unavailable for this kernel, `apt install
     linux-modules-extra-$(uname -r)` (or the closest available version)
     and reboot the VM, or `modprobe nls_utf8` after installing to avoid
     a reboot if the module loads without one.
  3. Failing that, UTF-8 is arguably CIFS's *implicit* default when no
     `iocharset` is given on modern kernels -- the real bug may be
     specifically in how `mount.cifs`/`cifs-utils` on this Ubuntu 24.04
     image resolves the `iocharset=utf8` request to a module load instead
     of recognizing UTF-8 as needing no translation module at all. Worth
     checking `cifs-utils` package version and whether a newer/older
     version behaves differently before touching kernel modules.
  4. As a last resort, side-step the whole CIFS layer for this
     specific need: since `pi4-master`'s manual mount reads the files
     correctly, compare its exact `mount.cifs` invocation/options against
     the CSI driver's to find what's actually different beyond
     `iocharset` (e.g. `vers=`, `sec=`, `nounix`) that makes one client
     succeed and the other fail.
- This does **not** block normal Syncthing operation. Everything except
  the ~66 (Projects) / ~782 (Syncthing folder) files with diacritics in
  their names syncs correctly. Safe to leave as a known issue.
## 2026-07-26 (resolution) -- nls_utf8 fix deployed, CIFS mojibake resolved

Picked back up the deferred fix from earlier today. Root cause was
already confirmed (missing `nls_utf8` kernel module on `k3s-burst-worker`
causing `iocharset=utf8` mount attempts to fail with `mount error(79):
Can not access a needed shared library`).

**Fix:**
```bash
# on k3s-burst-worker, via a privileged debug pod:
kubectl debug node/k3s-burst-worker -it --image=busybox --profile=sysadmin \
  -- nsenter -t 1 -m -u -i -n -p -- bash

apt update
apt install -y linux-modules-extra-$(uname -r)
modprobe nls_utf8
lsmod | grep nls_utf8   # confirms loaded, no reboot needed
```
`linux-modules-extra-6.8.0-134-generic` (113 MB) carries `nls_utf8.ko.zst`
plus a handful of other `nls_*` modules not included in the base
`linux-modules` package on this Ubuntu 24.04 cloud image. Installing it
also flagged a pending kernel upgrade (running `134`, latest available
`136`) -- unrelated to this fix, left for a separate maintenance window
since a reboot would be needed and nothing here requires it.

With the module loadable, recreated the PV (same delete/patch-finalizer/
recreate dance as the rest of today, PV name and `claimRef` preserved so
the existing PVC re-bound automatically) with `iocharset=utf8` explicitly
set. This time the mount succeeded immediately (pod `Running` in ~2s,
vs. the earlier attempt that sat in `ContainerCreating` failing
repeatedly).

**Verification:**
```bash
kubectl exec -n syncthing deployment/syncthing -- sh -c \
  'find ".../Statystyki/" -maxdepth 1 -type f -printf "%f\n" | od -An -tx1'
```
now shows `c4 85` (correct UTF-8 `ą`) instead of `3f` (`?`). Both
folders' error counts dropped to 0 (`Projects`: 66 -> 0, `Syncthing`:
782 -> 0) within one scan cycle, no further intervention needed.

Also updated `smb-tank-bulk-syncthing` StorageClass's `mountOptions` to
include `iocharset=utf8` (delete+recreate, since `mountOptions` are
immutable) so any future PV provisioned against this class inherits the
fix automatically -- doesn't affect the already-fixed PV, which carries
its own explicit `mountOptions`.

**Side effect noticed while verifying:** ~121 `.sync-conflict-*` files
appeared, nearly all inside `AnalitykBiznesowy_AI_Team_v2/.browser-profile/`
-- a Chrome/Firefox profile that's apparently being synced alongside the
actual project files. Browser cache/session files change constantly on
whichever device is active, so this directory generates sync conflicts
by nature. **Follow-up, not yet done:** add `(?d)**/.browser-profile/` to
the `Projects` folder's ignore patterns -- this data has no business being
synced in the first place. One conflict outside that directory
(`Obudowa_Nas.sync-conflict-...FCStd`, a FreeCAD project file) is a
genuine conflict worth reviewing by hand rather than blanket-ignoring.

**Status: resolved.** No more known mojibake/encoding issues on this
share. The `nls_utf8` gap was specific to `k3s-burst-worker`'s Ubuntu
24.04 cloud image -- worth checking whether the same module is present
on the other nodes (`pi4-master`, `pi4-worker2`, `g3-worker3`) before
scheduling any future SMB-mounting workload there, rather than
rediscovering this the same way.

## 2026-08-02 -- g3-worker3 power draw +70% (unrelated to same-day Terraform work)

**Symptom:** Home Assistant power monitor showed `g3-worker3` climbing from
~9W baseline to a sustained ~17-20W starting 2026-07-26, initially suspected
to be caused by that day's Terraform/MinIO/Uptime Kuma work (wrong guess --
those pods were consuming <1% CPU each when checked).

**Root cause:** the Firefox/Selkies remote-desktop sidecar attached to the
qBittorrent deployment had accumulated multiple browser tabs left open
since 2026-07-21 and 2026-07-25 (visible via `ps aux` inside the container:
several `-contentproc ... tab` processes dating back days, main firefox
process showing 1139+ minutes of accumulated CPU time). Selkies' own video
pipeline was correctly idle (`No display clients connected`, confirmed in
container logs) -- the load was coming from the browser tabs themselves,
not from encoding/streaming.

**Fix:** `kubectl rollout restart deployment/qbittorrent -n qbittorrent`
-- CPU idle on the node went from ~34-42% back to ~89% within a couple of
minutes of the new pod stabilizing.

**Lesson for next time:** this sidecar has no automatic tab-cleanup or
periodic restart. If used for tracker logins, close tabs after use, or
add a scheduled CronJob to restart the qbittorrent deployment weekly to
bound how long stray tabs can accumulate. Also a good reminder: when
diagnosing a resource spike, check actual per-pod/per-process usage
(`kubectl top`, `ps aux` inside the container) before assuming it's
whatever was deployed most recently -- the timing coincidence with that
day's Terraform work was misleading.

## 2026-08-02 -- g3-worker3 power draw +70%, traced via Grafana to stale Firefox sidecar tabs (unrelated to same-day Terraform work)

**Symptom:** Home Assistant power monitor showed `g3-worker3` climbing from
~9W baseline to a sustained ~17-20W starting 2026-07-26. Initially
suspected same-day Terraform/MinIO/Uptime Kuma work -- wrong guess, those
pods were consuming <0.002 cores each.

**Root cause:** the Firefox/Selkies remote-desktop sidecar attached to
qBittorrent had accumulated browser tabs left open since 2026-07-21 and
2026-07-25 (`ps aux` inside the container showed several `-contentproc
... tab` processes dating back days, main firefox process at 1139+
minutes accumulated CPU). Selkies' video pipeline was correctly idle
(confirmed via container logs) -- the load was the browser tabs
themselves, not encoding/streaming.

**Diagnosis path:** initially done the hard way via SSH + `top` +
`ps aux` on the node. Afterward, cross-checked in Grafana's built-in
"Kubernetes / Compute Resources / Node (Pods)" dashboard
(`kube-prometheus-stack` ships this out of the box, no extra
configuration needed) -- sorted the per-pod CPU table and got a clean,
quantified confirmation: the stale qBittorrent pod was at 0.284 cores
vs. 0.055 for the freshly restarted one, ~5x higher than any other pod
on the node.

**Fix:** `kubectl rollout restart deployment/qbittorrent -n qbittorrent`
-- node CPU idle went from ~34-42% back to ~89% within minutes.

**Lesson for next time:** this sidecar has no automatic tab-cleanup or
periodic restart -- close tabs after use, or add a scheduled CronJob to
restart the deployment weekly. Bigger lesson: **use Grafana's existing
Node (Pods) dashboard first** for this class of problem instead of
manual SSH digging -- the data was already there, just not the habit of
checking it first. A follow-up worth doing: configure a Prometheus alert
rule (per-pod CPU sustained above a threshold) so this surfaces as a
notification instead of being noticed a week later on the power monitor.

## 2026-08-23 -- Vaultwarden real TLS cert via Hostido AutoSSL, two blockers

Replacing the self-signed `mkcert` cert on the Vaultwarden Ingress with a
real Let's Encrypt certificate for `vault.analitykbiznesowy.pl` (issued
through Hostido's DirectAdmin panel, not a live ACME client on the
cluster -- see [setup/07-vaultwarden-tls-hostido.md](../setup/07-vaultwarden-tls-hostido.md)
for the full architecture) hit two separate blockers before it worked.

### Hostido blocking `.well-known/acme-challenge/`

Manual HTTP-01 validation kept returning `404` for any file placed under
`.well-known/acme-challenge/` in `public_html`, regardless of filename or
extension, while the rest of the site served files normally -- a
server-side rule on Hostido's shared hosting blocking dotfile-prefixed
directories. No client-side setting fixed it; **Hostido support fixed it**
after a ticket. Ended up abandoning manual HTTP-01 anyway in favor of
Hostido's built-in AutoSSL panel (Certyfikaty SSL -> "Uzyskaj automatyczny
certyfikat od dostawcy ACME"), which sidesteps `.well-known` entirely.

### Cert installs fine, but native apps reject it: `unable to get local issuer certificate`

After installing the AutoSSL cert as the `vaultwarden-tls` Secret, TLS
handshakes failed with `unable to get local issuer certificate` from an
outdated curl image and, critically, the **native Android Bitwarden app**
-- while modern desktop browsers accepted the same cert fine.

**Root cause:** Let's Encrypt issued the cert from the new `YR2`
intermediate, chaining to a brand-new root, `ISRG Root YR` (generated
Sept 2025). Per Let's Encrypt's own docs, that root (and its sibling
`ISRG Root YE`) is not yet included in most Root Program trust stores --
browsers with their own frequently-updated root store (Chrome/Android's
Google Root Store) had already added it, but clients relying on the
OS-level system trust store (the Bitwarden app) hadn't.

**Fix:** Let's Encrypt also publishes a cross-signed version of Root YR,
signed by the old, universally-trusted `ISRG Root X1`
(`https://letsencrypt.org/certs/gen-y/root-yr-by-x1.pem`). Serving
`fullchain.pem` as leaf -> intermediate `YR2` -> Root YR (cross-signed by
X1) -- 3 certificates total -- gives every client a valid path to a root
it already trusts, without needing to know about `ISRG Root YR` at all.

```bash
openssl s_client -connect <traefik-lb-ip>:443 -servername vault.analitykbiznesowy.pl \
  -showcerts </dev/null 2>/dev/null | grep -c "BEGIN CERTIFICATE"
# -> 3
```

**Verification:** the native Bitwarden Android app connecting and syncing
with no cert error was the actual acceptance test here -- it's the
strictest client in the chain (OS trust store only, no bundled root
updates), stricter than any browser check.

**Lesson for next time:** "modern client accepts it" isn't sufficient
verification for a new Let's Encrypt root rollout -- test against a
client that uses the OS-level trust store with no independent root
bundle, since that's exactly the gap a cross-signed intermediate closes
and a browser-only check would miss entirely.
