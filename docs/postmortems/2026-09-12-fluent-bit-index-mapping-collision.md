# Postmortem: fluent-bit shipping failure — shared index dynamic mapping collision

**Date:** 2026-09-12
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV3 (logging pipeline degraded, no impact on the services being logged)

## Summary
3 of 4 `fluent-bit` DaemonSet pods had been failing their readiness probe
for at least 38 days (pod age), with the OpenSearch output plugin unable to
flush any chunk — tens of thousands of failed retries with zero connection
errors. Root cause: the shared `fluent-bit` OpenSearch index had no explicit
field mapping, and `fluent-bit`'s Kubernetes filter flattens every pod's
parsed JSON log fields directly onto the top-level document. A one-off
Helm-hook Job (`kube-webhook-certgen`, from a `kube-prometheus-stack`
upgrade) logged structured JSON with generic field names (`error`,
`errors`, `items`), which got indexed like any other log and permanently
locked those field names to types that unrelated application logs later
collided with — causing every subsequent document sharing those names to be
silently rejected for whichever nodes' pods happened to log them.

## Impact
Logging pipeline degraded for an unknown but multi-week duration: only 2 of
4 nodes were successfully shipping logs to OpenSearch/OpenSearch Dashboards.
No impact on the applications being logged — this was purely an
observability gap, discovered only because the 2026-09-12 Alertmanager
routing fix (see companion postmortem) finally let the existing
`KubePodNotReady` alert reach a human for the first time.

## Timeline
All times CEST, 2026-09-12.

| Time | Event |
|------|-------|
| ~13:25 | First real alert notification ever delivered (immediately after the Alertmanager fix) surfaced `KubePodNotReady` for `fluent-bit-b2xx4` and `fluent-bit-qcqhf` |
| ~15:20 | Began diagnosis: `kubectl logs` showed endless `failed to flush chunk ... retry` / `chunk ... cannot be retried` against the `opensearch.0` output, with `input=tail.0` reading fine |
| ~15:22 | Confirmed OpenSearch itself was healthy: `_cluster/health` returned `yellow` (expected for a single-node cluster, not a real problem) via HTTP — ruled out the initial TLS-mismatch hypothesis (fluent-bit's config already correctly had `tls Off`) |
| ~15:25 | Ruled out missing authentication — `_cluster/health` returned `200` with **no** credentials at all |
| ~15:28 | Confirmed the `fluent-bit` index itself was healthy and actively growing (3.8M docs, 1.5GB) — proved this wasn't a systemic OpenSearch problem, only specific to 2 of 4 shipper pods |
| ~15:30 | Ruled out resource starvation — `kubectl top pod` showed trivial CPU/memory usage on all 4 pods |
| ~15:35 | Ruled out network/VXLAN packet-size issues — a temporary debug pod on the affected node successfully POSTed a 6000-byte payload to OpenSearch in 9ms |
| ~15:40 | Ruled out node clock skew — all 4 nodes' UTC clocks agreed within seconds |
| ~15:42 | Ruled out a stuck/corrupted local retry buffer — deleted the pod; the DaemonSet-recreated replacement failed identically within 3 minutes, proving the failure is live and reproducible, not stale backlog |
| ~15:45 | fluent-bit's own `/api/v1/metrics` showed the real signal: `opensearch.0` output had `errors: 0` (no connection-level failures) but `retries_failed: 142`, `dropped_records: 244` — pointed at per-document bulk rejection, not connectivity |
| ~15:50 | Inspected the `fluent-bit` index's dynamic mapping: found clearly non-application field names (`items.create._index`, `error.type`, `configuration_name`, `failure_policy`) |
| ~15:52 | Queried OpenSearch directly for a real document containing `configuration_name` — traced it to `kube-webhook-certgen` (`github.com/jkroepke/kube-webhook-certgen`), a legitimate one-off Job run during a `kube-prometheus-stack` Helm upgrade, logging structured JSON about patching admission webhook configurations |
| ~16:05 | Root cause confirmed: any application's JSON log field sharing a name with fields OpenSearch had already type-locked from an unrelated pod's log (here, `kube-webhook-certgen`) gets permanently rejected — explains the node-specific pattern (whichever pods/Jobs happened to run on `pi4-master`/`pi4-worker2` first collided) |
| ~16:15 | Created an OpenSearch index template for `fluent-bit-*` mapping a new `log_processed` field as `object, enabled: false` (first attempt used `type: flattened`, rejected — this OpenSearch build has no handler for that mapper type) |
| ~16:26 | Updated `helm/fluent-bit/values.yaml`: added `Merge_Log_Key log_processed` to the Kubernetes filter (nests parsed app JSON under one fixed, protected key instead of flattening to the top level) and switched the output to daily-rotated indices (`Logstash_Format`/`Logstash_Prefix`/`Logstash_DateFormat`) instead of one single, already-poisoned index |
| ~16:27 | `helm upgrade fluent-bit` applied; all 4 DaemonSet pods reached `1/1 Ready` within ~90 seconds, including both previously-failing ones |
| ~16:28 | Verified: new index `fluent-bit-2026.09.12` auto-created via the template, 246 documents and growing, zero errors |

## Root Cause
The `fluent-bit` → OpenSearch pipeline used a single, un-templated index for
every pod's logs cluster-wide, with the Kubernetes filter's `Merge_Log On`
flattening each pod's parsed JSON fields directly onto the top-level
document. OpenSearch's default dynamic mapping locks a field's type on
first sight. A one-off Job (`kube-webhook-certgen`) logged fields like
`error`/`errors` with types that didn't match what regular application
pods on other nodes later sent for fields of the same name — every
document from a pod using the "wrong" type for an already-claimed field
name was permanently rejected by OpenSearch's bulk API as a per-document
mapping error, which fluent-bit correctly treated as non-retryable and
eventually gave up on (`cannot be retried`), while continuing to generate
and fail on new chunks indefinitely.

## Trigger
Not a single event — latent since whichever point a Job's structured JSON
log first collided with a field name/type a normal application log also
used. Made visible only once alerting itself started working (see companion
postmortem), not by any change to fluent-bit or OpenSearch on this date.

## Detection
Indirect: the same-day Alertmanager routing fix was what let the
already-firing `KubePodNotReady` alert reach a human for the first time in
the cluster's history.

## Resolution
1. Worked strictly bottom-up, testing and ruling out one full layer at a
   time (TLS → auth → index health → resources → network, including
   specifically testing payloads larger than typical MTU → clock sync →
   stuck local state) before concluding the failure was data-level, not
   infrastructure-level — each wrong hypothesis was cheap to test and
   genuinely eliminated rather than assumed away.
2. Used fluent-bit's own `/api/v1/metrics` endpoint to distinguish
   "connection failures" from "per-document rejections" — the single most
   useful signal in the whole investigation, since it immediately ruled out
   an entire class of network/auth hypotheses at once.
3. Traced the actual offending field to its real source via a live
   OpenSearch query (`_search?q=configuration_name:*`) rather than guessing
   from the mapping alone — confirmed the true origin (`kube-webhook-certgen`)
   before designing a fix.
4. Fixed structurally, not cosmetically: isolating merged application JSON
   under one dedicated, `enabled:false` key makes this entire class of
   collision impossible going forward, for any future application, not just
   the one that happened to trigger this incident.
5. Verified with real, live evidence at each fix step (pod readiness,
   `_cat/indices` health, growing `docs.count`) rather than declaring
   success after the `helm upgrade` command alone returned cleanly.

## Action Items
- [ ] Decide a retention/cleanup policy for the old, pre-fix `fluent-bit`
      index (3.8M docs, 1.5GB, now effectively abandoned/read-only) — keep
      as historical archive or delete; not resolved today.
- [ ] Document the `Merge_Log_Key` + index-template pattern in
      `docs/KUBERNETES.md` as the required approach for any future
      shared-index logging pipeline, so this doesn't get silently
      reintroduced by a future Helm values change.
- [ ] Now that notifications actually work, consider a Prometheus alert on
      OpenSearch/fluent-bit indexing error rates directly — today's
      detection depended on `KubePodNotReady` catching the symptom at the
      pod level; a metric on the actual indexing failure would catch this
      class of bug faster and more specifically next time.

## Lessons Learned
- A shared, multi-tenant index with no explicit mapping strategy is a
  structural time bomb: whichever application uses a generic field name
  first "wins" that type forever, and every other application that later
  uses the same name differently silently loses — with no warning until
  something depends on the losing side.
- `errors: 0` in a delivery-layer metric does not mean "nothing is wrong" —
  it specifically rules out *connection*-level failure and should
  immediately redirect attention to data/application-level rejection
  instead, rather than being read as "everything is fine."
- Fixing one gap (alerting) directly surfaced the next one (this incident)
  within the same hour — postmortem action items compound, and closing the
  first blind spot is often what makes the next one visible at all.
