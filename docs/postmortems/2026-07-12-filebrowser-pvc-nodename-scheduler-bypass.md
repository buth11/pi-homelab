# Postmortem: FileBrowser PVC stuck Pending forever -- nodeName silently bypasses the scheduler

**Date:** 2026-07-12
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV3 (single service unavailable, no data at risk)

## Summary
A `local-path` PVC for FileBrowser never bound, stuck indefinitely on
`WaitForFirstConsumer` with no provisioning event ever appearing, even
though the identical StorageClass had just successfully provisioned a
volume for another Deployment minutes earlier. Root cause: FileBrowser's
Deployment used `spec.nodeName` instead of `spec.nodeSelector`, which
bypasses the scheduler entirely -- and it's the scheduler that annotates
a `WaitForFirstConsumer` PVC with the selected node, which never
happened.

## Impact
FileBrowser unavailable until fixed (session-length, under an hour of
active debugging). No data at risk.

## Timeline
| Time | Event |
|---|---|
| T+0 | FileBrowser's `local-path` PVC created, expected to bind on first pod schedule per `WaitForFirstConsumer` semantics |
| T+0 to T+30m | PVC remains `Pending`; `kubectl describe pvc` shows only repeating `WaitForFirstConsumer`, no `Provisioning` event ever appears in `local-path-provisioner` logs |
| T+10m, T+20m | Provisioner pod restarted, PVC deleted and recreated multiple times -- no change |
| T+25m | Noted the *exact same StorageClass* had just bound a volume for qBittorrent minutes earlier -- rules out a StorageClass-wide problem |
| T+28m | `kubectl describe pod` for FileBrowser shows `PodScheduled: True` and the correct node -- looks completely normal at first glance |
| T+30m | Checked `Node-Selectors` field specifically in the pod description -- empty, despite the pod clearly running on the intended node |
| T+32m | Root cause identified: Deployment uses `spec.nodeName: g3-worker3` directly instead of `spec.nodeSelector` |
| T+35m | Changed Deployment to `nodeSelector: {kubernetes.io/hostname: g3-worker3}`; the next PVC created bound immediately |

## Root Cause
`nodeName` bypasses the Kubernetes scheduler entirely -- the kubelet on
the named node picks the pod up directly without scheduler involvement.
But it's the *scheduler* that annotates a `WaitForFirstConsumer` PVC
with `volume.kubernetes.io/selected-node` once it places a pod that
needs it. Since the scheduler was never involved for this pod, that
annotation never got written, and `local-path-provisioner` (which
watches for that annotation to trigger provisioning) had nothing to act
on.

## Trigger
FileBrowser's Deployment was the only one in the cluster using
`nodeName` for placement instead of `nodeSelector` -- likely written
early on before the `nodeSelector` convention was established for the
rest of the cluster, and never revisited.

## Detection
Manual, and misleading for a while: the pod itself looked entirely
normal (`PodScheduled: True`, correct node) which made this look
identical to a provisioner malfunction until the `Node-Selectors` field
specifically was checked and found empty.

## Resolution
Changed FileBrowser's Deployment from `nodeName: g3-worker3` to
`nodeSelector: {kubernetes.io/hostname: g3-worker3}` -- functionally
equivalent pod placement, but goes through the scheduler. The very next
PVC bound immediately with no other changes.

## Action Items
- [x] Audited the rest of the repo (`grep -rn nodeName k8s/`) and
      confirmed FileBrowser was the only Deployment using `nodeName`;
      no other instances of this class of bug exist in the cluster
- [ ] Consider a CI lint step that flags any future use of `nodeName` in
      committed manifests, given how easy this is to reintroduce and
      how misleading the symptom is

## Lessons Learned
- `nodeName` and `nodeSelector` often produce the same pod placement
  result, which makes them look interchangeable -- they are not.
  `nodeName` skips scheduling-time logic entirely, silently breaking
  anything that depends on the scheduler doing work at bind time (like
  `WaitForFirstConsumer` volume binding)
- When a resource is stuck in a state that "should" resolve itself
  (`WaitForFirstConsumer`, `Pending`), check every field the mechanism
  actually depends on, not just the fields that look normal at a glance
  -- `PodScheduled: True` was true, but the specific annotation the
  provisioner needed was never written
- Restarting a controller/provisioner and recreating the stuck resource
  are reasonable first troubleshooting steps, but when neither changes
  anything after multiple attempts, that's a signal to stop repeating
  the same action and instead re-examine assumptions about *why* the
  mechanism should work
