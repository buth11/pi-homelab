# Postmortem: A fix for one PV's stale CIFS cache left a sibling PV's mount dead

**Date:** 2026-07-26
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV3 (a second Syncthing folder went unreadable; no data
loss, self-inflicted by the previous day's fix)

## Summary
The prior day's fix for a stale CIFS directory cache (force-unmounting a
`globalmount` on one node) was correct for the specific PV it targeted,
but a *different* PVC on the same node, using the same CIFS share and
StorageClass, had its own independent `globalmount` that was left in a
broken state as a side effect. The following day, that second folder
showed `permission denied` and fell through to the node's local
filesystem instead of the intended network share. A clean scale-down/
scale-up forced a fresh mount and resolved it with no data loss.

## Impact
A second Syncthing folder (`Syncthing`, ~78 GiB) appeared to lose access
to its data for the affected node -- `df -h` inside the pod reported a
local block device instead of the expected CIFS mount. No actual data
was lost; the underlying share was untouched throughout.

## Timeline
| Time | Event |
|---|---|
| 2026-07-25 (previous day) | Force-unmounted a specific `globalmount` on `k3s-burst-worker` to fix a stale dentry cache affecting the `Projects` folder (see prior postmortem) -- correct and effective for that PV |
| 2026-07-26 | The `Syncthing` folder (separate PVC, same node, same CIFS share, same StorageClass) shows `Niezsynchronizowane` / `permission denied` on `stat /data/Syncthing` |
| +5m | `kubectl exec ... -- ls -la /data/` returns an almost-empty directory owned by `root:root` instead of the expected ~78 GiB tree |
| +10m | `df -h /data` from inside the pod reports `/dev/sda1` -- a local block device -- instead of the expected `//192.168.50.21/syncthing` CIFS mount |
| +15m | `mount | grep syncthing` on the node confirms zero CIFS mounts present at all for that share, on either PV -- the mount had gone stale entirely, not just cached-and-wrong |
| +20m | Applied the same recovery pattern as the previous day, this time without needing a manual `umount -f` since nothing was mounted to begin with: scale to 0, wait for full removal, scale back to 1 |
| +22m | `df -h /data` inside the new pod correctly shows the CIFS mount (`//192.168.50.21/syncthing 11T 70G 11T 1% /data`) |

## Root Cause
The previous day's force-unmount targeted one specific `globalmount`
path, correctly fixing the PV it was scoped to. But `syncthing-data`
(the PVC backing the separate `Syncthing` folder) used the same node,
same CIFS share, and same StorageClass via an *independent*
`globalmount`. The most likely explanation: the node's kubelet didn't
notice this second PV's bind-mount had gone stale until some later
trigger (day boundary, a routine scan, or simply elapsed time) caused a
re-check -- at which point, with no live `globalmount` underneath it to
bind to, the pod's `/data` silently fell through to the node's local
filesystem instead of failing loudly with a clear error.

## Trigger
Directly caused by the previous day's remediation for an unrelated
incident -- a force-unmount scoped correctly to one PV had an
unanticipated side effect on a sibling PV sharing the same underlying
CIFS share and node.

## Detection
Manual -- noticed via the Syncthing web UI showing a folder state change
and confirmed via `df -h` inside the pod.

## Resolution
```bash
kubectl scale deployment syncthing -n syncthing --replicas=0
kubectl get pods -n syncthing -w   # wait for full removal
kubectl scale deployment syncthing -n syncthing --replicas=1
kubectl exec -n syncthing deployment/syncthing -- df -h /data
# -> //192.168.50.21/syncthing   11T   70G   11T   1%   /data
```
No manual `umount -f` was needed this time, since the mount had already
gone entirely absent rather than merely stale.

**Side effect noticed while diagnosing:** `/data`'s root also contained
leftover Syncthing config files (`cert.pem`, `config.xml`, `key.pem`,
`syncthing.lock`, `index-v2/`) from a brief prior period when a
different config PVC pointed at the same share -- the StorageClass
doesn't carve out a distinct `subDir` per PVC, so two PVCs against it
can silently share the same root directory. Harmless (that config PVC
was no longer referenced by anything live) but cleaned up to avoid
future confusion.

## Action Items
- [x] Documented: a force-unmount fix for a CIFS caching problem is
      scoped to the specific `globalmount` path, not to the share as a
      whole -- any other PV pointed at the same source share, even from
      the same node, needs to be checked (and likely bounced) too
- [x] Established a verification step for future CIFS remounts: run
      `mount | grep <share>` on the node and cross-check against every
      PV using that StorageClass before considering an incident closed:
```bash
      kubectl get pv -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.csi.volumeAttributes.source}{"\n"}{end}' | grep <share>
```
- [ ] Consider whether the SMB StorageClass should be reconfigured to
      use a distinct `subDir` per PVC, to prevent any future accidental
      config/data collision on the same share root

## Lessons Learned
- A fix scoped correctly to the resource it was meant to address can
  still have unintended effects on sibling resources sharing
  infrastructure underneath (same node, same network share, same
  storage class) -- "fixed" for one PV does not mean "unaffected" for
  every other PV using the same underlying mechanism
- After any infrastructure-level remediation (force-unmounts, cache
  clears, node-level interventions), it's worth proactively checking
  every other resource that shares the same underlying mechanism,
  rather than waiting for each one to individually surface a symptom on
  its own schedule
- A resource silently falling through to a different backing store
  (local disk instead of network share) with no loud error is far more
  dangerous than an outright failure -- it can look like reduced
  capacity or unexpected content rather than an obvious outage
