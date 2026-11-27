# Postmortem: power outage exposes the cluster's missing cold-start path

**Date:** 2026-10-29 (outage) / 2026-10-30 (recovery)
**Author:** Bartosz Suszko
**Status:** In progress. Services restored 2026-10-30 07:30 UTC. Surviving the
next power loss without manual intervention is **not solved yet**; see the
P0/P1 action items.
**Severity:** SEV2 (multi-service outage of ~7h50m, no alert at any point
during it, no data loss)

## Summary
A mains power cut (~08:20–10:47 UTC) took the whole homelab down. When power
came back, only the two Raspberry Pis booted on their own; `g3-worker3`, the
Proxmox host (and with it the `k3s-burst-worker` VM) and TrueNAS stayed off
until they were powered on by hand at ~16:09 UTC. **No notification was sent
at any point**, because Prometheus and Alertmanager live on exactly the
nodes that stayed down, and the external dead man's switch (healthchecks.io)
produced no notification either. That part is still unexplained. Bringing
the machines back up then surfaced two more failures: Syncthing could not
mount its SMB volume (the `nls_utf8` gap from 2026-09-09 recurred), and MinIO
could not start because `minio/minio` is no longer pullable from Docker Hub.
That in turn broke the nightly Vaultwarden backup. Both were fixed the next
morning: kernel module via Ansible, MinIO via a private GHCR copy of the
image still cached on g3.

The real finding is not any single bug. **The cluster has never had a
designed or tested cold start.** Every recovery mechanism it does have
(alerting, the boot-time Ansible watcher, Terraform) quietly depends on
something that a power cut also takes down.

## Impact
| What | Down from | Back at | Duration | Why |
|---|---|---|---|---|
| Everything | ~08:20 | 10:47 | ~2h27m | no power |
| Pi-hole DNS, Linkding, Homelab Dashboard, market-exporter (Pi nodes) | ~08:20 | ~10:47 | ~2h27m | Pis auto-boot |
| **Vaultwarden**, Jellyfin, arr-stack, qBittorrent, MinIO, Uptime Kuma (`g3-worker3`) | ~08:20 | ~16:10 | **~7h50m** | g3 does not power on after AC loss |
| Prometheus, Grafana, Syncthing (`k3s-burst-worker`) | ~08:20 | ~16:14 | ~7h54m | Proxmox host does not power on after AC loss |
| **Alertmanager** (`g3-worker3`) | ~08:20 | ~16:10 | ~7h50m | as g3 |
| Syncthing (after its node was back) | 16:14 | 06:4x (+1d) | ~14h30m | CIFS mount error(79), missing `nls_utf8` |
| MinIO (after its node was back) | 16:10 | 07:27 (+1d) | ~15h17m | image no longer pullable |
| Vaultwarden backup | nightly run 03:32 (+1d) failed | manual run 07:30 (+1d) | 1 missed backup; newest good backup was ~28h old at the worst point | MinIO down |

- **Error budgets:** Vaultwarden's 99.5% / 30-day SLO allows ~3h36m; this
  incident alone used ~7h50m, so **~2.2× the monthly budget**. Internal DNS
  (99.9%, ~43m) was down ~2h27m, so **~3.4× the budget**.
- **The SLO dashboards will not show this.** The burn is computed from
  `probe_success`, and nothing scraped probes while Prometheus was down.
  `avg_over_time` over a window with no samples ignores the gap, so the
  outage reads as zero errors. This is a measurement blind spot in its own
  right (see action items).
- Vaultwarden clients kept working from their offline vault cache, so there
  was no loss of access to passwords, but no sync or edits for ~8h.
- No data loss. MinIO data (local-path on g3) and Vaultwarden data were intact.

## Timeline
All times UTC. Times marked ~ are reconstructed from journal boundaries.

| Time | Event |
|------|-------|
| **2026-10-29** | |
| ~08:20 | Mains power lost. Last journal entries: `k3s-burst-worker` 08:20:05, `g3-worker3` 08:22:14 |
| 10:46:55 | Power restored. `pi4-master` and `pi4-worker2` boot by themselves. `g3-worker3`, the Proxmox host (→ `k3s-burst-worker` VM) and TrueNAS stay **off** |
| 10:47+ | Pi-hole, Linkding, dashboard, market-exporter running again. Prometheus (local-path PV pinned to `k3s-burst-worker`) and Alertmanager (local-path PV pinned to `g3-worker3`) **cannot be rescheduled** onto the Pis. **No alerting from here on** |
| 10:47+ | TrueNAS share `/mnt/truenas-syncthing` fails to mount on `pi4-master` (TrueNAS is off) and is never retried |
| ~11:48 | `node-boot-watcher` on `pi4-master` reacts to the Pis' kubelet `Starting` events and runs Ansible: `cd: /mnt/truenas-syncthing/Pi-Homelab/ansible: No such file or directory`, exit 1. Also `mkdir: cannot create directory '/run/ansible-node-watcher': Permission denied`. Failures go to the journal only |
| — | healthchecks.io stops receiving the `Watchdog` heartbeat from ~08:20. **No notification reached the owner.** Cause not yet established |
| ~16:05 | Owner returns home and notices g3, the burst worker and TrueNAS are off; powers them on by hand |
| 16:08:55 | `g3-worker3` boots; node Ready 16:09:10 |
| 16:09:08 | Watcher runs Ansible for `g3-worker3`: same `cd` failure, exit 1 |
| ~16:10 | Alertmanager running again (on g3) |
| 16:13:42 | `k3s-burst-worker` VM boots (kernel `6.8.0-147-generic`, first boot on it); node Ready 16:13:56 |
| ~16:14 | Prometheus running again. Alerting pipeline restored |
| 16:14+ | Syncthing pod: `FailedMount … mount error(79): Can not access a needed shared library`, retried 433× over 14h |
| 16:1x+ | MinIO pod: `ImagePullBackOff` for `minio/minio:latest`, retried 3818× over 14h |
| 16:29:38 | `KubePodNotReady` (minio, syncthing) fires → ntfy |
| 17:14:38 | `KubeContainerWaiting` (syncthing, `ContainerCreating`) fires → ntfy |
| evening | Owner asks why there was no alert. Diagnosis: Prometheus on the burst worker, Alertmanager on g3, both pinned there by local-path PVs |
| **2026-10-30** | |
| 03:32–03:33 | Nightly `vaultwarden-backup` fails 3 attempts (`backoffLimit: 2`): `EndpointConnectionError … minio.minio.svc.cluster.local:9000` |
| 04:40:20 | `VaultwardenBackupStale` (critical) fires → ntfy |
| ~06:40 | `ansible-playbook site.yml --limit k3s-burst-worker`: installs `linux-modules-extra` for 6.8.0-147, loads `nls_utf8`. Syncthing mounts and goes Running on the kubelet's next retry |
| 06:46:42 | `crictl pull docker.io/minio/minio:latest` on g3: `pull access denied, repository does not exist or may require authorization`. Image still present in g3's containerd cache (`69b2ec208575b`) |
| ~06:50 | Deployment patched to `imagePullPolicy: IfNotPresent` (out-of-band, Terraform state backend is MinIO itself). Kubelet **still** pulls |
| ~07:02 | Patched to `imagePullPolicy: Never` as a diagnostic: `Container image "minio/minio:latest" is not present with pull policy of Never`, although `crictl inspecti` finds it and a matching kubelet pull record exists. Root cause of the refusal not established |
| ~07:24 | Cached image tagged and pushed from g3 to private `ghcr.io/buth11/minio:cached-2026-10-30` (amd64 only), using a short-lived `write:packages` token. Pull secret `ghcr-pull` (read-only token) created in `minio`. Deployment patched to the GHCR image + `IfNotPresent` + `imagePullSecrets` |
| ~07:27 | MinIO Running (`RELEASE.2025-09-07T16-13-09Z`) |
| 07:30:32 | Manual Vaultwarden backup succeeds (`vaultwarden-20261030T073032Z.tar.gz.age`, 310 691 bytes). All services restored |

## Root Cause
There is no designed, tested path for the cluster to come back from a cold
start. Concretely, eight independent gaps lined up:

1. **Three machines do not power on after AC loss.** `g3-worker3`, the
   Proxmox host and TrueNAS stay off when power returns. The Pis have no
   power button, so they always boot. That is the only reason anything came
   back by itself.
2. **The alerting pipeline runs on the least resilient nodes.** Prometheus
   and Alertmanager have `local-path` PVCs and no `nodeSelector`, so they
   were pinned to whichever node first provisioned their volume (burst
   worker, g3). The PV node affinity then stops the scheduler from moving
   them, which is the same mechanism as the 2026-07-25 Syncthing postmortem.
   Independent of power cuts: **g3 is routinely shut down from the dashboard,
   and every time it is, alerts stop being delivered.**
3. **The dead man's switch did not notify.** It is the one layer built for
   exactly this case (the whole cluster gone), and it stayed silent. Why is
   still open: no integration on the check, an integration nobody watches
   (e-mail), or the period/grace settings.
4. **The self-healing watcher depends on TrueNAS and fails silently.**
   `node-boot-watcher` runs the playbook from `/mnt/truenas-syncthing/...`.
   During a cold start TrueNAS is the last thing up, and the mount never
   retries, so the watcher failed on every node all day with only a journal
   line to show for it. Its debounce state also lives in `/run`, which is
   wiped at boot and is not writable by the service user.
5. **Kernel upgrades land without `linux-modules-extra` (recurrence of
   2026-09-09).** unattended-upgrades installed `6.8.0-147` on the burst
   worker without the extra-modules package that carries `nls_utf8`. The
   first reboot onto it, here forced by the power cut, breaks CIFS mounts.
   The 2026-09-09 fix (Ansible role + watcher) handles this after the fact,
   but item 4 disabled that mechanism.
6. **MinIO ran `:latest` from an upstream that stopped publishing.** With
   `:latest` the default pull policy is `Always`, so every new pod start
   needed Docker Hub, and `docker.io/minio/minio` is no longer anonymously
   pullable. The image only existed in g3's local cache.
7. **The Terraform state backend lives in the service Terraform manages.**
   MinIO is defined in `terraform/minio.tf`, and the S3 backend is MinIO
   itself. With MinIO down the fix had to be an out-of-band `kubectl patch`,
   which now has to be reconciled back into code.
8. **The kubelet refused a locally cached image** under both `IfNotPresent`
   and `Never`, although containerd held a complete copy and a matching
   kubelet pull record (`/var/lib/kubelet/image_manager`) existed. Best
   hypothesis: the kubelet's pull-credential verification (KEP-2535) treating
   the image as not present. Not proven. Worked around by re-pulling from
   GHCR, which writes a fresh record.

## Trigger
Mains power loss at ~08:20 UTC on 2026-10-29, the first full cold start of
the cluster since it was built.

## Detection
- **During the outage (08:20–16:09): no detection.** Nothing inside the house
  could alert (no power, then no monitoring), and the external dead man's
  switch did not reach the owner.
- **Actual detection:** the owner noticed by eye on returning home, ~7h45m
  after the start.
- **After recovery:** alerting worked as designed. `KubePodNotReady`,
  `KubeContainerWaiting` and `VaultwardenBackupStale` all fired and reached
  ntfy. What was missing was alerting *during* the outage, and alerting
  *about the recovery tooling itself* (the watcher's failures).
- **Also blind:** Uptime Kuma runs on g3 too, so the second monitoring tool
  shared the same fate.

## Resolution
1. Powered on g3, the Proxmox host and TrueNAS by hand (~16:05–16:14 UTC).
2. `cd ansible && ansible-playbook site.yml --limit k3s-burst-worker`:
   installed `linux-modules-extra-6.8.0-147-generic`, loaded `nls_utf8`.
   Syncthing recovered on the kubelet's next mount retry.
3. MinIO, all out-of-band (Terraform backend unavailable):
   - confirmed upstream image unpullable (`crictl pull` → access denied);
   - `imagePullPolicy` → `IfNotPresent`, then `Never`: kubelet still refused the cached copy;
   - `ctr images tag` + `ctr images push --platform linux/amd64` of the cached image to private `ghcr.io/buth11/minio:cached-2026-10-30`;
   - `kubectl create secret docker-registry ghcr-pull -n minio` with a `read:packages`-only token;
   - `kubectl patch` deployment: GHCR image, `IfNotPresent`, `imagePullSecrets: [ghcr-pull]`.
4. `kubectl create job --from=cronjob/vaultwarden-backup` → backup OK; stale-backup alert resolves.

**Out-of-band state that must be reconciled** (see action items): MinIO
deployment image, pull policy and pull secret differ from `terraform/minio.tf`;
Secret `ghcr-pull` exists only on the live cluster.

## Action Items
Priority: **P0** = until done, the next power cut repeats this incident;
**P1** = removes a hidden dependency; **P2** = hygiene.

### P0: get told, and come back up by yourself
- [ ] **Explain and fix the silent dead man's switch.** In healthchecks.io: did
      the check go *down* on 2026-10-29, which integrations does it have,
      what period/grace. Attach a push integration that is actually watched
      (ntfy topic or phone app), then **test it**: scale Alertmanager to 0,
      confirm the notification arrives within the grace period, scale back.
- [ ] **Auto power-on after AC loss:** BIOS/UEFI *Restore on AC Power Loss →
      Power On* on `g3-worker3`, the Proxmox host and TrueNAS. Proxmox:
      `k3s-burst-worker` VM *Start at boot* (`qm set <id> --onboot 1`).
      Verify by pulling the plug on each one.
- [ ] **Move Alertmanager (and decide on Prometheus) to always-on nodes.**
      Alertmanager is light enough for a Pi: `nodeSelector` to a Pi + new PVC.
      Prometheus on a Pi is a trade-off (4 GB RAM, SD-card I/O → shorter
      retention); write the decision down either way. Goal: alerting survives
      g3 being off, which happens routinely, not only in power cuts.
- [ ] **Cold-start test** once the above is done: power the whole lab off at
      the wall, restore, and verify unattended recovery against a checklist
      (nodes Ready, monitoring up, alert delivered, backup target reachable).
      Write the procedure into `docs/RUNBOOKS.md` and repeat it periodically.

### P1: remove the hidden dependencies
- [ ] **`node-boot-watcher`:** run the playbook from a local git checkout on
      `pi4-master` instead of the TrueNAS mount; replace the `/run` state dir
      with `RuntimeDirectory=ansible-node-watcher` in the unit; make a failed
      run visible (node-exporter textfile metric + alert, or an ntfy push).
- [ ] **Stop the `nls_utf8` recurrence at the source:** make sure kernel
      upgrades on Ubuntu nodes pull in `linux-modules-extra` (check which
      kernel metapackage the burst worker tracks), or have the
      `kernel-modules` role install it for every installed kernel, not just
      the running one.
- [ ] **TrueNAS mount on `pi4-master` must retry:** systemd automount
      (`x-systemd.automount`) or a mount unit with restart, so a late TrueNAS
      does not leave it unmounted all day.
- [ ] **Reconcile Terraform with the live MinIO deployment:** in
      `terraform/minio.tf` set `image = "ghcr.io/buth11/minio:cached-2026-10-30"`,
      `image_pull_policy = "IfNotPresent"`, `image_pull_secrets { name = "ghcr-pull" }`;
      decide how `ghcr-pull` is managed (Terraform `sensitive` var vs.
      live-only like other secrets). `terraform plan` must show no changes.
- [ ] **Terraform state backend independent of MinIO-in-cluster** (or a
      documented break-glass: periodic `terraform state pull` copy outside
      MinIO plus a procedure for running against it).
- [ ] **Third copy of the MinIO image:** `ctr images export` to a `.tar` on
      TrueNAS (snapshotted dataset), sha256 recorded, restore command in
      `docs/RUNBOOKS.md`. GHCR + g3 cache alone share the "one account / one
      node" failure modes.
- [ ] **Decide MinIO's future:** the cached build is from 2025-09 and gets no
      security fixes. Options: build from source in CI and push to GHCR, or
      move to another S3-compatible store.

### P2: hygiene and observability
- [ ] **SLO blind spot:** make "no data" count as "bad". Alert on
      `absent()`/staleness of the probe series, and evaluate SLOs from the
      outside or from a component that does not share the outage. Confirmed
      2026-11-03: `avg_over_time(probe_success{job="vaultwarden"}[30d])`
      returns exactly 1 despite this outage.
- [ ] **SLO window longer than retention:** the SLO rules use `[30d]`, but
      Prometheus retention is 7d, so the "30-day" figure covers at most 7 days.
      Raise retention (check disk: 8Gi PVC) or add remote storage.
- [ ] **Move Uptime Kuma off g3** (or accept and document that it is not an
      independent monitor).
- [ ] **`VaultwardenBackupFailed` is sticky:** it keys on any failed backup Job
      object, so it kept paging every 4h after the successful manual backup
      at 07:30 until the failed Job was deleted by hand. Rewrite it to fire
      only when the most recent scheduled run has not succeeded, e.g.
      `max(kube_cronjob_status_last_schedule_time{…}) > max(kube_cronjob_status_last_successful_time{…})`
      with a `for:` longer than a normal run. Add a promtool unit test.
- [ ] **Vaultwarden backup job installs packages with `pip` at runtime**: bake
      dependencies into an image so a backup does not depend on PyPI.
- [ ] **Investigate the kubelet's refusal of the cached image** (root-cause
      item 8) with kubelet verbosity on a test pod, so a cached image can be
      trusted in an emergency. Record the finding in `docs/troubleshooting.md`.
- [ ] Delete the `ghcr-push-minio-2026-10-30` token on GitHub (single-use,
      `write:packages`). Calendar reminder to rotate the `read:packages` pull
      token before it expires (90 days).
- [ ] Optional: a small UPS for the router, Pis and switch, so short cuts do
      not cause a cold start at all and the router can still send alerts.

## Lessons Learned
- **A recovery mechanism that shares a failure domain with the failure is
  not a recovery mechanism.** Alerting on g3, the watcher's playbook on
  TrueNAS, Terraform's state in MinIO: each worked in every scenario tested
  so far, and each was unavailable in the one scenario that took everything
  down at once.
- **Silent failure of the safety net is worse than no safety net.** The
  watcher failed on every node all day and nobody knew. Anything meant to
  fix things automatically must also report when it could not.
- **Test the cold start, not just the warm restart.** Individual reboots
  never exposed any of this. Only a full, simultaneous power loss did. A
  deliberate wall-plug test is cheap compared with finding out at 08:20 on a
  workday.
- **`:latest` is a dependency on someone else's future decisions.** Pin
  versions and keep a copy of every image you cannot rebuild.
- **The SLO said nothing because the thing measuring it was down.** Missing
  data has to read as an outage, not as perfection.
- **A fix that has to be repeated is a fix that did not finish.** The
  2026-09-09 `nls_utf8` postmortem fixed the symptom and added automation,
  but did not stop kernel upgrades arriving without the module.

## Related
- [2026-09-09 k3s-burst-worker CIFS mount failure](2026-09-09-k3s-burst-worker-cifs-mount-failure.md): same `nls_utf8` gap, first occurrence
- [2026-07-25 Syncthing config loss on node migration](2026-07-25-syncthing-config-loss-node-migration.md): local-path PV node affinity
- [2026-09-12 Alertmanager null routing](2026-09-12-alertmanager-null-routing.md): earlier "alerts silently not delivered" incident
