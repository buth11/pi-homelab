# Postmortem: g3-worker3 sustained +70% power draw

**Date:** 2026-08-02
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV3 (no service impact, resource waste + early warning sign ignored for a week)

## Summary
A home power monitor showed node `g3-worker3` climbing from a ~9W
baseline to a sustained ~17-20W starting six days earlier. Initial
hypothesis (same-day Terraform/Helm work) was wrong. Actual cause: a
browser sidecar container attached to the qBittorrent deployment had
accumulated multiple stale, never-closed tabs over two weeks,
consuming CPU continuously even with no active viewer connected.
Fixed with a pod restart; confirmed via Grafana per-pod CPU metrics.

## Impact
- ~15W higher continuous power draw on one node for approximately 7 days
  before being noticed and fixed
- No user-facing service degradation — the affected node still met all
  workload requirements throughout

## Timeline
| Time | Event |
|---|---|
| 2026-07-26 (approx.) | Power draw begins climbing, per Home Assistant historical graph — not noticed at the time |
| 2026-08-02 08:00 | Power monitor graph reviewed retrospectively, anomaly spotted |
| 2026-08-02 08:05 | Initial (incorrect) hypothesis: that day's Terraform/MinIO/Uptime Kuma deployment work caused it |
| 2026-08-02 08:10 | `kubectl top pods` shows all newly-deployed pods at <1% CPU — hypothesis rejected |
| 2026-08-02 08:15 | SSH + `top` on the node identifies a `firefox` process at 68-90% CPU |
| 2026-08-02 08:20 | `ps aux` inside the container shows multiple `-contentproc ... tab` processes dated 2026-07-21 and 2026-07-25; main process shows 1139+ minutes of accumulated CPU time |
| 2026-08-02 08:22 | `kubectl rollout restart deployment/qbittorrent -n qbittorrent` |
| 2026-08-02 08:25 | Node CPU idle returns from ~35-42% busy to ~89% idle within minutes |
| 2026-08-02 (later) | Cross-verified via Grafana's built-in "Kubernetes / Compute Resources / Node (Pods)" dashboard: stale pod measured at 0.284 cores vs. 0.055 for the freshly restarted one, ~5x higher than any other pod on the node |

## Root Cause
The Firefox/Selkies remote-desktop sidecar container (used for logging
into torrent trackers via a browser accessible through the cluster) had
no automatic tab-cleanup or scheduled restart. Tabs left open across
multiple sessions over roughly two weeks accumulated CPU usage from
background JS execution, even though the remote-viewing pipeline itself
was correctly idle (`No display clients connected` in the container's
own logs).

## Trigger
No single trigger — gradual accumulation of browser tabs across several
manual sessions over two weeks, never cleaned up.

## Detection
Manual, and later than it should have been: noticed by chance while
reviewing an unrelated power monitor graph, a full week after the
drift began. No alert existed at the time for per-node or per-pod
resource anomalies.

## Resolution
`kubectl rollout restart deployment/qbittorrent -n qbittorrent` —
replaced the pod, clearing all accumulated browser state. Verified fix
via both raw `top` on the node and Grafana's per-pod CPU panel.

## Action Items
- [x] Built a Prometheus alert (`NodeCPU24hAboveWeeklyBaseline`) comparing
      each node's trailing-24h CPU average against its prior 6-day
      baseline, routed to a push notification — this exact class of
      slow-drift issue would now page within ~24-30h instead of being
      found by chance a week later
- [ ] Add a scheduled weekly restart (CronJob) for the qbittorrent
      deployment to bound how long stray browser tabs can accumulate,
      independent of whether anyone remembers to close them manually
- [ ] Check Grafana's existing dashboards *first* for this class of
      problem going forward — the per-pod CPU data was already being
      collected, it just wasn't the first place looked

## Lessons Learned
- Temporal correlation is not causation: the power spike happened to be
  investigated the same day as unrelated infrastructure work, which was
  the first (wrong) hypothesis. Checking actual per-process resource
  usage before assuming a cause based on timing avoided a wasted
  afternoon chasing the wrong component
- A sidecar container with no lifecycle management (no auto-restart, no
  session limits) is a resource leak waiting to happen, even when its
  primary function (screen streaming) is correctly idling
- Existing observability tooling (Grafana, already deployed) should be
  the first stop for this kind of investigation, not raw SSH — the
  postmortem was written up faster the second time around specifically
  because the Grafana panel gave a clean, quantified confirmation number
  instead of relying on `top` snapshots alone
