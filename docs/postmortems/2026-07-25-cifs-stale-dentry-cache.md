# Postmortem: CIFS directory-listing cache survived pod restart and drop_caches, required a forced remount

**Date:** 2026-07-25
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV3 (Syncthing repeatedly reported already-fixed files as
errors; no data impact)

## Summary
After deleting mojibake duplicate files directly on the NAS from a
different client, Syncthing on the affected node kept reporting the
exact same "already fixed" files as errors -- surviving a full
application-level rescan, a pod restart, and an attempted
`drop_caches`. The stale state was in the Linux kernel's CIFS directory
entry cache, scoped to the node, not the pod -- which meant a pod
restart never touched it, and a `chroot`-only debug session lacked the
actual host privileges needed to clear it. A forced unmount of the
underlying CIFS mount, letting the CSI driver recreate it clean, fixed
it immediately.

## Impact
Syncthing folder showed a stubborn, unchanging error count (66) for
files that had already been correctly fixed on the NAS, for
approximately the duration of the debugging session (under an hour).

## Timeline
| Time | Event |
|---|---|
| T+0 | Mojibake duplicate files deleted directly on the TrueNAS share via a different client (`pi4-master`) |
| T+5m | Syncthing on `k3s-burst-worker` continues reporting the exact same files as scan errors |
| T+10m | Triggered a full `db/scan` via the Syncthing REST API -- error count unchanged, `state: idle` (Syncthing genuinely believed a fresh scan still saw these files) |
| T+15m | `kubectl rollout restart` on the Syncthing Deployment -- error count still unchanged after the new pod stabilized |
| T+20m | Attempted `echo 3 > /proc/sys/vm/drop_caches` inside a `kubectl debug node/... --image=busybox -- chroot /host bash` session -- no change |
| T+25m | Recognized the cache in question must be scoped below the pod level -- investigated the CIFS mount's cache behavior directly |
| T+30m | Root cause identified: the mount's `actimeo=1` option only bounds *attribute* cache TTL, not the CIFS directory entry cache under `cache=strict` mode, and that cache lives at the node's kernel level via a `globalmount` shared across any pod scheduled there |
| T+35m | Additionally realized `chroot /host` changes the visible root filesystem but not the mount namespace -- writing to `/proc/sys/vm/drop_caches` there doesn't reliably reach the host's actual cache |
| T+40m | Scaled the Deployment to 0, waited for full pod removal, entered the host's actual namespaces via `kubectl debug ... --profile=sysadmin -- nsenter -t 1 -m -u -i -n -p -- bash`, force-unmounted the specific `globalmount`, scaled back to 1 |
| T+42m | Error count dropped from 66 to 0 immediately after the forced unmount/remount cycle |

## Root Cause
The CSI SMB mount's `actimeo=1` option only bounds attribute cache TTL
(file size, mtime) -- it does not bound the CIFS **directory entry
cache** under `cache=strict` mode. This cache is scoped to the node's
kernel, not the pod: the mount is a `globalmount` that any new pod
scheduled onto that node reuses as-is, so a pod restart never touches
it. A plain `chroot /host` session changes only the visible root
filesystem, not the mount namespace, which is why writing to
`/proc/sys/vm/drop_caches` from inside it silently did nothing.

## Trigger
A file deletion performed by a client other than the one whose CIFS
mount was showing the stale state -- the node's kernel-level directory
cache had no mechanism to learn about the external change within the
timeframe checked.

## Detection
Manual -- the Syncthing REST API's persistently unchanged error count
across multiple remediation attempts was the signal that something
below the application layer was stale.

## Resolution
Force-unmounted the stale `globalmount` and let the CSI driver recreate
it clean on next schedule:
```bash
kubectl scale deployment <name> -n <ns> --replicas=0
kubectl get pods -n <ns> -w    # wait for full removal
kubectl debug node/<node> -it --image=busybox --profile=sysadmin \
  -- nsenter -t 1 -m -u -i -n -p -- bash
mount | grep <share-name>
umount -f /var/lib/kubelet/plugins/kubernetes.io/csi/smb.csi.k8s.io/<hash>/globalmount
kubectl scale deployment <name> -n <ns> --replicas=1
```

## Action Items
- [x] Documented that `kubectl debug node/<x> -it --image=busybox --
      chroot /host bash` is sufficient only for read-only host
      filesystem inspection -- anything needing real host privileges
      (unmounting, `nsenter` into other namespaces, effective
      `drop_caches`) needs `--profile=sysadmin` or an equivalent
      privileged manifest
- [ ] Note for any future CIFS-backed PV: if a file deleted/changed by a
      different client isn't reflected after a reasonable propagation
      window, suspect the dentry cache before suspecting the application

## Lessons Learned
- Application-level remediation (rescans, pod restarts) cannot fix a
  problem that lives below the application, in the kernel's cache for a
  specific mount -- recognizing *which layer* owns a piece of state is
  necessary before choosing where to intervene
- A command that "appears" to succeed silently (like `drop_caches`
  inside a `chroot`-only shell, which no-ops without error) is more
  dangerous during debugging than one that fails loudly, since it gives
  false confidence that an approach has been tried and ruled out
- CIFS's `globalmount` model means cache state is shared across every
  pod scheduled onto a node, not scoped per-pod -- a pod restart is not
  equivalent to a mount refresh for network filesystems mounted this way
