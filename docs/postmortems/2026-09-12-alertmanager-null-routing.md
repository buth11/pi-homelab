# Postmortem: Alertmanager routing sent nearly everything to null, hidden behind a two-owner Secret conflict

**Date:** 2026-09-12
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV2 (no direct outage, but a total monitoring blind spot — every built-in Kubernetes alert rule had effectively been disabled since install)

## Summary
Three prior postmortems this week (2026-09-08, 2026-09-09, 2026-09-10) each
independently concluded "no alerting exists for this failure class." Checking
that assumption found the opposite: the standard `kube-prometheus-stack`
alert rules for exactly these failure classes (`KubePodCrashLooping`,
`KubeContainerWaiting`, `KubeDaemonSetNotScheduled`) already existed and were
firing — they just never reached a human, because Alertmanager's routing
tree sent almost everything to a `"null"` receiver by default, with only one
hand-picked alertname ever routed to notifications. Fixing the routing
uncovered a second, independent bug: two separate Helm releases were both
trying to own the same Alertmanager config Secret, which blocked the fix
until resolved.

## Impact
No direct service outage. The impact was a total loss of alerting signal:
roughly 50 built-in Prometheus alert rules had been evaluating correctly
but silently discarded since the cluster's alerting stack was installed.
This directly caused three real incidents this week (duplicate LoadBalancer
controller, kernel module drift, and — discovered immediately after this
fix — a fluent-bit shipping failure) to go undetected until found by hand.

## Timeline
All times CEST, 2026-09-12.

| Time | Event |
|------|-------|
| ~11:00 | Reviewing open action items from 2026-09-08/09/10 postmortems, checked whether `KubePodCrashLooping`/`KubeDaemonSetNotScheduled`-class rules already exist in `kube-prometheus-stack` — confirmed they do |
| ~11:05 | Checked Alertmanager's live routing config: default route → `receiver: "null"`, with exactly one carved-out route (`NodeCPU24hAboveWeeklyBaseline` → `ntfy`) — everything else, including all built-in rules, silently dropped |
| ~11:10 | Checked currently-active alerts: 9 firing, including 3 `severity: critical` (`KubeControllerManagerDown`, `KubeSchedulerDown`, `KubeProxyDown`) — identified as permanent false positives, since k3s bundles these components into one binary and never exposes them on the ports `kube-prometheus-stack`'s default ServiceMonitors expect |
| ~11:15 | Attempted `helm upgrade kube-prometheus-stack ... --reuse-values` to add `kubeScheduler/kubeControllerManager/kubeProxy: enabled: false` — failed with a nil-pointer template error, a known `--reuse-values` interaction bug with this chart |
| ~11:20 | Confirmed via `helm get values` that the committed `helm-values/kube-prometheus-stack.yaml` already matched 100% of what was live (nothing hidden via out-of-band `--set`) — safe to drop `--reuse-values` entirely |
| ~11:24 | Second failure: `Apply failed with 2 conflicts ... Secret alertmanager-kube-prometheus-stack-alertmanager` — two Helm releases, `node-alerting` and `kube-prometheus-stack`, both trying to own the same Secret object |
| ~11:24 | Root cause: `helm/node-alerting` hand-templated this Secret directly (copying the chart's own default `inhibit_rules` verbatim plus custom routing), duplicating a mechanism the `kube-prometheus-stack` chart already provides natively via its own `alertmanager.config` value |
| ~11:24–11:24 | Consolidated ownership: moved the full desired Alertmanager config (severity-based routing, inhibit rules) into `helm-values/kube-prometheus-stack.yaml` under `alertmanager.config`; deleted `helm/node-alerting/templates/alertmanager-secret.yaml`; kept the ntfy webhook URL (contains a token) out of git, supplied only via `--set-string` at upgrade time |
| 11:21–11:25 | `helm upgrade node-alerting` (removed the conflicting Secret) → confirmed gone via `kubectl get secret` (`NotFound`) → `helm upgrade kube-prometheus-stack` succeeded cleanly |
| ~11:26–11:30 | Verified: `kubeControllerManager`/`kubeScheduler`/`kubeProxy` alert rules fully removed from Prometheus's own `/api/v1/rules` (not just muted); the 3 stale critical alerts auto-resolved in Alertmanager ~25 minutes later, matching expected reload timing |
| ~13:25 | Confirmed via ntfy.sh web history: real warning-severity alerts (`KubePodNotReady` for fluent-bit, `KubeDaemonSetRolloutStuck`, `KubeJobFailed`) successfully delivered end-to-end |
| ~17:00–17:25 | User-reported "nothing arriving since" traced to a misunderstanding (re-polling old messages, not sending a new test) — resolved by posting a synthetic test alert directly via Alertmanager's `/api/v2/alerts` API, confirmed delivered within `group_wait` (~30s) |

## Root Cause
Two independent, compounding issues:
1. **Alertmanager routing** defaulted every alert to a `"null"` receiver except one manually carved-out exception. This was never deliberately "misconfigured" so much as never actually finished — a single custom alert got wired up and the rest was left on the chart's inert default.
2. **Duplicate Secret ownership**: `helm/node-alerting` was created to add one custom alert + routing, but instead of using `kube-prometheus-stack`'s native `alertmanager.config` value, it hand-rolled a full replacement Secret with the exact same name the parent chart also tries to manage — a collision that had simply never been triggered before, because nothing had forced `kube-prometheus-stack` to actually reconcile ownership of that specific object until this change.

## Trigger
Manually reviewing whether the action items from three prior postmortems
("no alerting exists for X") were actually true, rather than accepting the
prior postmortems' conclusions at face value.

## Detection
Manual. No alert detected the absence of alerting — by definition, a
routing-to-null bug cannot page anyone about itself.

## Resolution
1. Verified the built-in alert rules already existed before writing anything
   new (avoided duplicating ~3 custom rules that the chart already ships).
2. Diagnosed and disabled the k3s-incompatible `kubeScheduler`/
   `kubeControllerManager`/`kubeProxy` monitoring at the source (chart
   values), not by muting the resulting alerts in Alertmanager — the
   `ServiceMonitor` and its alert rule group are both gone, not just quiet.
3. When the first `helm upgrade` failed, checked `helm get values` before
   guessing further — confirmed nothing depended on `--reuse-values` before
   removing it.
4. When the second `helm upgrade` failed with a Secret ownership conflict,
   consolidated to a single owner (`kube-prometheus-stack`'s native
   mechanism) rather than reaching for a workaround (e.g., renaming one
   Secret and pointing `configSecret` at it) — same "pick one owner"
   discipline as the 2026-09-10 duplicate-LoadBalancer-controller fix.
5. Verified with a real, on-demand synthetic alert posted directly to
   Alertmanager's API, rather than waiting for an organic alert to test the
   pipeline — confirmed delivery within `group_wait` deterministically.

## Action Items
- [ ] Set up ntfy push notifications on the phone app (browser notification
      permission was the reason a real, successfully-delivered alert wasn't
      noticed immediately — not an infrastructure problem, but worth
      closing so it doesn't cause a "did this actually work?" scare again).
- [ ] Note in `docs/KUBERNETES.md` that `alertmanager.config` in
      `helm-values/kube-prometheus-stack.yaml` is the **sole** owner of
      Alertmanager routing — nothing else should template a Secret named
      `alertmanager-kube-prometheus-stack-alertmanager` again.

## Lessons Learned
- A previous postmortem's "no alerting exists for this" is a hypothesis,
  not a fact — checking it directly (`kubectl get prometheusrule`) took five
  minutes and completely changed the fix (routing config, not new PromQL).
- An alert rule firing and a human being notified are two different systems
  that both have to be independently verified — this cluster had the first
  working (mostly) the whole time and zero visibility into the second being
  broken.
- This is the second time this week the actual root cause was "two
  independent management paths targeting the same object" (after k3s
  ServiceLB vs. MetalLB on 2026-09-10) — worth treating as a pattern to
  actively watch for in this cluster, not a one-off.
- Testing a notification pipeline by triggering a real, controlled synthetic
  event (POST directly to the API) gives a fast, deterministic answer —
  much faster than waiting on and reasoning about organic alert timing.
