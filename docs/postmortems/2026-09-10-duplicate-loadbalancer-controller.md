# Postmortem: duplicate LoadBalancer controller flooding the scheduler

**Date:** 2026-09-10
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV3 (no traffic impact — pure scheduler waste and clutter)

## Summary
`kubectl get pods -n kube-system` showed roughly a dozen `svclb-*` pods stuck
`Pending`, some for as long as 98 days. Root cause: k3s ships a built-in
LoadBalancer controller (`ServiceLB`, aka klipper-lb) that was never disabled
when MetalLB was installed as the cluster's real LoadBalancer implementation.
Both controllers reacted to every `type: LoadBalancer` Service — MetalLB
correctly assigned working external IPs via L2/ARP, while k3s's own
controller also tried to stand up a `hostPort`-binding DaemonSet pod per
node for the same services, losing the port race almost everywhere and
sitting `Pending` forever. Fixed by disabling `servicelb` via
`/etc/rancher/k3s/config.yaml` and restarting k3s on the control-plane.

## Impact
None on actual traffic — MetalLB was already serving every LoadBalancer
Service correctly the entire time. Impact was confined to scheduler churn:
one affected pod alone (`svclb-argocd-server`) logged **10,963**
`FailedScheduling` events over 38 days, and the pattern repeated across
~14 services × up to 4 nodes each — a large, sustained volume of pointless
scheduling attempts that nobody had noticed until a routine `kubectl get
pods` scan turned it up.

## Timeline
All times CEST, 2026-09-10.

| Time | Event |
|------|-------|
| ~19:10 | Routine `kubectl get pods -n kube-system` turned up ~30 `svclb-*` pods in `Pending`, ages ranging 37–98 days |
| ~19:12 | `kubectl describe pod svclb-argocd-server-...` showed `FailedScheduling` (×10,963 over 38d): `1 node(s) didn't have free ports for the requested pod ports, 3 node(s) didn't satisfy plugin(s) [NodeAffinity]` |
| ~19:15 | `kubectl get svc -A \| grep LoadBalancer` confirmed all 17 LoadBalancer Services already had working `EXTERNAL-IP`s from MetalLB's pool (`192.168.50.50–80`) — ruled out "LoadBalancer IPs are broken," pointed at a second, redundant controller instead |
| ~19:23 | Root cause confirmed: k3s's built-in `ServiceLB` was never disabled at install time, coexisting with MetalLB and fighting it for the same `hostPort`s |
| ~19:23 | `/etc/rancher/k3s/config.yaml` created on `pi4-master` with `disable: [servicelb]` — deliberately not touching the installer-generated `/etc/systemd/system/k3s.service`, which a future k3s upgrade could silently overwrite |
| 19:25:43 | `sudo systemctl restart k3s` on `pi4-master` |
| ~19:26 | `systemctl status k3s` confirmed `active (running)`; transient `configmap`/`secret` cache-sync errors in the log were expected post-restart noise, not a new problem |
| ~19:27 | `kubectl get nodes` — all 4 nodes `Ready`; `kubectl get pods -n kube-system \| grep svclb` — empty, the orphaned DaemonSets were cleaned up automatically once the owning controller stopped |
| ~19:27 | `kubectl get svc -A \| grep LoadBalancer` — all 17 services retained their exact same `EXTERNAL-IP`s, unchanged |
| ~19:28 | `curl` against three live IPs (Traefik, Pi-hole web, ArgoCD server) returned real HTTP responses (404/403/200), confirming traffic still flows post-fix, not just that the Service object looked correct |

## Root Cause
MetalLB was installed as the cluster's LoadBalancer implementation, but
k3s's own bundled `ServiceLB` (klipper-lb) was never explicitly disabled at
install time via `--disable servicelb`. Both controllers watch the same
`type: LoadBalancer` Services and both try to provision something for each
one. MetalLB does this correctly via L2/ARP announcement of an IP from its
pool — no host-level port binding required. k3s's `ServiceLB` does it the
older way: a DaemonSet with one pod per node, each binding the service's
port directly on the host (`hostPort`). Once Traefik's own `svclb` pod (or
another service's) had already claimed a given host port on a given node,
every other service's `svclb` pod competing for that same port on that
node had nowhere to go and sat `Pending` indefinitely — which is exactly
why only some replicas of each `svclb` DaemonSet were `Running` and the
rest were permanently `Pending`.

## Trigger
Not a single event — this was latent misconfiguration from whenever MetalLB
was first installed alongside k3s's default LoadBalancer support, silently
accumulating more affected services (and more failed-scheduling volume)
every time a new `type: LoadBalancer` Service was created afterward.

## Detection
Manual — spotted by eye during an unrelated `kubectl get pods -n
kube-system` check, not by any alert. The 10,963-event count on a single
pod shows this had been silently running in the background for over a
month with zero visibility.

## Resolution
1. Ruled out "MetalLB is broken" first, since that would have been the far
   more disruptive hypothesis — confirmed every LoadBalancer Service already
   had a working `EXTERNAL-IP` before looking any further.
2. Read the actual `FailedScheduling` event text instead of guessing at a
   cause — the message named both failure modes directly (port conflict,
   node affinity mismatch), which is what pointed at "a second controller
   fighting for the same ports" rather than a resource or quota problem.
3. Fixed at the correct layer: `/etc/rancher/k3s/config.yaml`, which k3s
   merges with its systemd-unit flags at startup, instead of hand-editing
   the installer-generated unit file — the same "fix the source of truth,
   not the live/generated artifact" discipline used in the 2026-09-08 and
   2026-09-09 postmortems.
4. Verified in three layers, not just one: node health, whether the stale
   `svclb-*` objects actually cleared, whether the real `EXTERNAL-IP`s were
   untouched, and finally a live `curl` against real IPs to confirm actual
   traffic — config that merely *looks* right was not treated as proof.

## Action Items
- [ ] No alerting exists for a `FailedScheduling` event storm on a single
      pod persisting over days/weeks — this is the same detection gap noted
      in the 2026-09-08 and 2026-09-09 postmortems (`CrashLoopBackOff` /
      stuck-mount alerting). All three point at the same missing piece:
      Prometheus alerting on pod/scheduling health, not just node and
      resource metrics.
- [ ] Note in `docs/KUBERNETES.md` that `servicelb` is deliberately disabled
      via `/etc/rancher/k3s/config.yaml` on `pi4-master`, so a future
      cluster rebuild or node re-provisioning doesn't reintroduce this by
      omission.

## Lessons Learned
- A `Pending` pod that's been `Pending` for 98 days and never paged anyone
  is a strong signal the real service it's *for* doesn't actually depend on
  it — worth checking what's actually serving traffic before assuming a
  long-`Pending` pod is urgent, but it's still worth cleaning up rather than
  normalizing permanent scheduler noise.
- Two controllers can each be individually "working correctly" and still
  produce a real problem together — MetalLB was never at fault here, and
  k3s's `ServiceLB` was arguably "working as designed," but the combination
  was still wrong. Diagnosing this meant checking the whole path (Service →
  both controllers → actual traffic), not stopping at "well, the IP is
  there."
- The event count (10,963 over 38 days) turned an easy-to-dismiss cosmetic
  issue into a concrete, quantified cost — worth pulling that number
  specifically when deciding whether something "harmless-looking" is worth
  fixing now versus later.
