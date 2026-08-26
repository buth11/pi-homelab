# Runbooks

> Prescriptive "if X happens, do Y" procedures — the forward-looking
> counterpart to [troubleshooting.md](troubleshooting.md), which is a
> chronological log of what *already* went wrong. These runbooks were
> written by generalizing from that log and from
> [postmortems/](postmortems/); when a new incident reveals a repeatable
> procedure, add it here, not just to the log.
>
> (Named `RUNBOOKS.md` rather than `TROUBLESHOOTING.md` deliberately —
> `troubleshooting.md` already exists lowercase, and a same-name file
> differing only by case breaks on case-insensitive filesystems. Same
> content role, different file.)

## Pod stuck in `ContainerCreating` (SMB/CIFS-backed PVC)

**Symptom:** pod hangs in `ContainerCreating`, `kubectl describe pod`
shows a mount timeout or `error(13)` against an SMB source.

1. Confirm the SMB source is actually reachable: `smbclient -L
   //<source-host>` from a node.
2. Check the underlying share didn't move (physical disk swap, TrueNAS
   pool reorg) — if it did, the fix is a new PVC pointed at the new
   share, not trying to resurrect the dead mount (see
   [troubleshooting.md, 2026-07-11](troubleshooting.md#2026-07-11----initial-truenasproxmox-integration-day)).
3. If the share is reachable but the mount is stale after a prior forced
   unmount elsewhere on the same node, see the CIFS globalmount runbook
   below — a dead mount for PV *A* can leave PV *B* on the same node
   unable to establish a fresh CIFS session.

## PVC stuck `Pending` forever

**Symptom:** PVC never binds, no clear error in `describe pvc`.

Check the pod spec for a hardcoded `spec.nodeName` — setting it directly
(instead of a `nodeSelector`) **bypasses the scheduler entirely**,
including the step that triggers `local-path`'s dynamic provisioning.
The fix is `nodeSelector`, not `nodeName` — see
[postmortems/2026-07-12-filebrowser-pvc-nodename-scheduler-bypass.md](postmortems/2026-07-12-filebrowser-pvc-nodename-scheduler-bypass.md).

## SQLite-backed app corrupted / crash-looping after a node move

**Symptom:** an app using SQLite for its own config/state (Syncthing,
Prowlarr, Sonarr, Radarr) breaks after its PVC moves onto SMB, or after
migrating to a new node while already on SMB.

Root cause is always the same: **CIFS/SMB doesn't support the file
locking SQLite's WAL mode needs.** Not a timing issue, not fixable with
retries. Fix: move the config PVC to `local-path`, pinned via
`nodeSelector` to wherever it's actually going to run. See
[postmortems/2026-07-25-syncthing-config-loss-node-migration.md](postmortems/2026-07-25-syncthing-config-loss-node-migration.md)
and the storage-decision writeup in
[setup/06-arr-stack.md](../setup/06-arr-stack.md#storage-decisions) for
the pattern applied preventively the second time around.

## CIFS directory listing stale / stuck after pod restart

**Symptom:** files that exist on the TrueNAS share don't show up inside
the pod, `drop_caches` doesn't help.

The kernel CIFS client cache can survive a pod restart. Forced remount
needed — full procedure (scale to 0, `nsenter` into PID 1's namespaces
since a plain `chroot` isn't enough for unmount, force-unmount the
`globalmount`, scale back up) is in
[troubleshooting.md, 2026-07-26](troubleshooting.md#2026-07-26----forced-unmount-from-a-previous-incident-left-a-different-pvs-cifs-mount-dead).
Note a forced unmount on one PV's mount can collaterally kill a
*different* PV's mount on the same node — check siblings after.

## Polish filenames showing as mojibake on SMB shares

**Symptom:** diacritics (ą, ć, ę, ł, ń, ó, ś, ź, ż) render as garbage or
`?` in filenames synced/copied onto TrueNAS SMB shares.

Root cause: missing `nls_utf8` kernel module support for the CIFS mount.
Fixed permanently via the `kernel-modules` Ansible role
(`ansible/roles/kernel-modules/`), which ensures the module persists
across reboots — if this resurfaces, check that role actually ran on the
affected node before re-diagnosing from scratch. Full original diagnosis:
[troubleshooting.md, 2026-07-26 (resolution)](troubleshooting.md#2026-07-26-resolution----nls_utf8-fix-deployed-cifs-mojibake-resolved).

## Node CPU usage spikes unexpectedly

**Symptom:** a node's CPU idle drops sharply with no corresponding
deploy/config change that day.

**Check Grafana's built-in "Kubernetes / Compute Resources / Node
(Pods)" dashboard first** (ships with `kube-prometheus-stack`, no extra
config) — sort the per-pod CPU table before doing anything else. The
2026-08-02 incident was traced this way in minutes after initially being
diagnosed the hard way via SSH + `top` + `ps aux`; the data was already
there. If it points at the Firefox/Selkies sidecar specifically, it's
almost certainly stale browser tabs accumulating CPU — restart the
deployment (`kubectl rollout restart deployment/qbittorrent -n
qbittorrent`), don't investigate further first.

## Vaultwarden TLS certificate approaching expiry

**Symptom:** `vaultwarden-tls` cert nearing its 90-day Let's Encrypt
expiry (currently **2026-11-22**).

There is no ACME client automating this — renewal is manual:
1. Re-issue via Hostido's DirectAdmin panel (Certyfikaty SSL → "Uzyskaj
   automatyczny certyfikat od dostawcy ACME").
2. Build `fullchain.pem` as leaf → `YR2` intermediate → Root YR
   cross-signed by X1 (3 certs) — **do not skip this**, a 2-cert chain
   with the bare `YR2` intermediate breaks native apps (Bitwarden
   Android) even though browsers accept it fine. Full explanation:
   [troubleshooting.md, 2026-08-23](troubleshooting.md#2026-08-23----vaultwarden-real-tls-cert-via-hostido-autossl-two-blockers).
3. Apply per [setup/07-vaultwarden-tls-hostido.md](../setup/07-vaultwarden-tls-hostido.md)
   (`kubectl create secret tls ... --dry-run=client -o yaml | kubectl
   apply -f -`), verify chain length is 3 with the `openssl s_client`
   one-liner in that doc, then test against the Bitwarden Android app
   specifically before considering it done.

## Pi-hole web password won't stay changed

**Symptom:** password set via the Pi-hole web UI reverts after a pod
restart.

This is expected given current config, not a bug to chase: `WEBPASSWORD`
is hardcoded in `k8s/pihole/deployment.yaml` and gets reapplied on every
container start (confirmed via logs — `Password ... set in config file`
appears on every restart). Until that's moved to a `Secret`, don't bother
changing it through the UI expecting it to persist — either accept the
env-var value or edit the manifest directly.

## g3-worker3 shutdown / wake cycle

Automated nightly: `shutdown-pods` CronJob (22:55, scales qBittorrent +
Jellyfin to 0) then `shutdown-g3` CronJob (23:00, SSH shutdown). Manual
equivalent:
```bash
kubectl scale deployment qbittorrent -n qbittorrent --replicas=0
kubectl scale deployment jellyfin -n jellyfin --replicas=0
ssh buth11@192.168.50.13 "sudo shutdown -h now"
```
Wake: send WoL to the MAC in `k8s/dashboard/configmap.yaml`
(`G3_MAC`), wait for `kubectl get node g3-worker3` to show `Ready`,
uncordon if it was cordoned, then scale both deployments back to 1. The
dashboard automates this end-to-end.

## Unexpected cluster action with no clear trigger

If a node reboots, a pod gets evicted/deleted, or a deployment gets
scaled and nobody remembers triggering it:

1. Check whether it correlates with `dashboard-backend` being up at the
   time (`kubectl logs -n dashboard deploy/dashboard-backend`) — every
   action it takes goes through `kubernetes` Python client calls that
   show up in its logs.
2. Cross-check `kubectl get events -A --sort-by=.lastTimestamp` around
   the same window for anything else that could explain it (ArgoCD
   self-heal, a CronJob, a manual `kubectl` session from another
   terminal) before assuming it was automated.
3. If internal service access controls are the suspected cause, rotate
   any credentials that service can reach and treat it as a live
   incident, not a config bug — see [SECURITY.md](SECURITY.md) for the
   access-control standard this is checked against.

## Rotating the dashboard's Basic Auth password

The dashboard (`http://192.168.50.58`) is gated by HTTP Basic Auth —
credential lives in the `dashboard-auth` Secret (`.htpasswd` key), never
committed. To rotate:

```bash
PASS=$(openssl rand -base64 18 | tr -d '/+=' | head -c 24)
echo "$PASS"   # save it (e.g. Vaultwarden) -- shown once
HASH=$(openssl passwd -apr1 "$PASS")
printf 'buth11:%s\n' "$HASH" > /tmp/htpasswd
kubectl create secret generic dashboard-auth -n dashboard \
  --from-file=.htpasswd=/tmp/htpasswd \
  --dry-run=client -o yaml | kubectl apply -f -
shred -u /tmp/htpasswd
kubectl rollout restart deployment/dashboard-frontend -n dashboard
```
Full commands also live as comments in
[k8s/dashboard/auth-secret.yaml](../k8s/dashboard/auth-secret.yaml).
