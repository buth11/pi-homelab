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

Expected: `FTLCONF_webserver_api_password` comes from the `pihole-auth`
Secret and overrides the UI on every container start. Pi-hole v6 ignores
the old v5 `WEBPASSWORD` variable entirely (it keeps whatever is in
`pihole.toml` on the PVC). Don't change the password in the UI; rotate the
Secret instead with the commands in
[k8s/pihole/auth-secret.yaml](../k8s/pihole/auth-secret.yaml).

## g3-worker3 shutdown / wake cycle

Shutdown and wake are driven from the Homelab Dashboard
(`http://192.168.50.58`). There is no nightly automation: the
`shutdown-pods` / `shutdown-g3` CronJobs were suspended by hand on
2026-06-08 after `shutdown-g3` failed with `BackoffLimitExceeded`, and were
removed on 2026-09-19 (their leftover failed Job was what raised
`KubeJobFailed` once alert routing was fixed). Manual equivalent:
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

The dashboard is gated by HTTP Basic Auth at the nginx frontend — the
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

## Rebooting the Proxmox host (new kernel installed)

Proxmox (`192.168.50.20`) hosts two VMs: Home Assistant (VM 100) and
`k3s-burst-worker` (VM 101). Both disks live on `tank-fast-nfs`
(TrueNAS, over the dedicated `10.10.10.x` link), so rebooting the host
takes down Home Assistant and one K3s node, but not TrueNAS itself.
Expect a few minutes of downtime for both.

**Before the reboot** (on `root@pve` unless noted):

1. Confirm a newer kernel is actually waiting: `uname -r` (running) vs
   `ls /boot | grep vmlinuz` (installed).
2. Confirm nothing long-running is in flight: `pgrep -af
   'rsync|vzdump'`. Empty output = safe. A reboot kills a running
   transfer or backup.
3. Confirm every VM that must come back has start-at-boot set: `qm list`,
   then `qm config <vmid> | grep onboot` for each. No output means
   `onboot` is off and the VM stays down after the reboot. Fix with
   `qm set <vmid> --onboot 1`. (On 2026-09-19 neither VM 100 nor 101 had
   it set.) VM 102 (`IT02`) is intentionally stopped; leave it.
4. Drain the K3s node that lives on this host (from a machine with
   `kubectl`):
   ```bash
   kubectl get pods -A -o wide --field-selector spec.nodeName=k3s-burst-worker
   kubectl drain k3s-burst-worker --ignore-daemonsets --delete-emptydir-data
   ```
   Pods with `local-path` PVCs (Prometheus, Grafana, Syncthing at the
   time of writing) are pinned to this node and will sit `Pending` for
   the whole window, so Prometheus stops scraping (no alerts fire from
   it) and Syncthing stops syncing. That is expected, not a failure.

**Reboot:** `reboot` on `root@pve`. Proxmox shuts guests down gracefully
(`pve-guests`) before restarting. **Do not `uncordon` yet.**

**After the host is back:**

1. On `root@pve`: `uname -r` (new version?), `systemctl --failed` (empty),
   `pvesm status` (`tank-fast-nfs` must be `active`, otherwise the VMs
   cannot start), `qm list` (100 and 101 `running`).
2. Home Assistant: check Zigbee devices are back (the USB passthrough is
   by vendor:product ID, so it should survive).
3. Only now: `kubectl get nodes`, then
   `kubectl uncordon k3s-burst-worker`, then
   `kubectl get pods -A | grep -v -E "Running|Completed"` until empty.
4. Right after boot a pod may show a transient `FailedMount` with
   `driver name smb.csi.k8s.io not found`: the pod started before the CSI
   SMB DaemonSet re-registered. Kubernetes retries and it clears on its
   own.
5. The `ansible-node-watcher` on `pi4-master` re-applies the kernel
   modules role when the node's kubelet starts. Confirm with
   `journalctl -u ansible-node-watcher -n 30` (this is the drift from
   [postmortems/2026-09-09-k3s-burst-worker-cifs-mount-failure.md](postmortems/2026-09-09-k3s-burst-worker-cifs-mount-failure.md)).

**Rollback:** old kernels stay installed. `proxmox-boot-tool kernel list`,
then `proxmox-boot-tool kernel pin <old-version>` and reboot again.
Proxmox is a mini-PC with no IPMI, so keep a monitor and keyboard within
reach: if the network doesn't come up there is no remote way in.

**Lesson from the 2026-09-19 reboot:** `uncordon` was run right after the
drain, before the host had actually rebooted. That cancelled the drain,
the pods moved back onto the node, and the reboot then killed them
without a clean eviction. It recovered only because the data sits on the
VM disk. Keep the node cordoned until the host is confirmed back.

## Upgrading K3s (minor/patch)

First done 2026-09-19: v1.35.5 → v1.36.4 on all four nodes. Order matters:
**control plane first, then workers; a kubelet must never be newer than the
apiserver.** Move one minor version at a time. Pick the target from
`https://update.k3s.io/v1-release/channels` (the `stable` channel is the
default choice).

**Before starting**

1. Nothing in the cluster should be unhealthy:
   `kubectl get pods -A | grep -v -E "Running|Completed"`.
2. Read what the installer will overwrite. It **rewrites the systemd unit
   from the arguments you pass now**, so any flag currently in `ExecStart`
   disappears unless it is in `config.yaml`:
   ```bash
   sudo systemctl cat k3s | sed -n '/^KillMode/p;/^ExecStart=/,$p'      # agents: k3s-agent
   sudo sed 's/=.*/=<hidden>/' /etc/systemd/system/k3s.service.env   # names only, values hidden
   sudo cat /etc/rancher/k3s/config.yaml
   ```
   Move every flag (here `--flannel-iface <nic>`) into `config.yaml` as
   `flannel-iface: <nic>` first. A wrong flannel interface can break pod
   networking cluster-wide (Tailscale is present on this network).
   Also check `KillMode=process`: it is what lets pods survive a K3s restart.
3. **Back up the master's datastore.** It is SQLite in WAL mode, so copying
   `state.db` alone is inconsistent. Stop K3s, copy the whole directory,
   start it again (pods keep running), and keep the old binary, since a
   minor downgrade needs old DB + old binary:
   ```bash
   BK=/var/backups/k3s-$(date +%Y%m%d)-pre-upgrade
   sudo mkdir -p "$BK" && sudo systemctl stop k3s
   sudo cp -a /var/lib/rancher/k3s/server/db "$BK/db"
   sudo cp -a /var/lib/rancher/k3s/server/token "$BK/token"
   sudo cp -a /usr/local/bin/k3s "$BK/k3s-old"
   sudo systemctl start k3s && sudo chmod -R go-rwx "$BK"
   ```
   The backup holds cluster secrets: keep it root-only, outside any
   Syncthing folder, and delete it once the cluster has been stable for a
   week.

**Upgrade** (each node, master first). No drain: most workloads here have
`local-path` volumes pinned to their node, so a drain would just leave them
`Pending` (Pi-hole DNS included), while restarting the K3s service does not
restart pods.

```bash
# master
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=<version> sh -

# every agent: re-supply K3S_URL/K3S_TOKEN by sourcing the existing env
# file, otherwise the installer rewrites it without them and the agent
# cannot rejoin (the token never appears on screen or in history)
sudo bash -c 'set -a; . /etc/systemd/system/k3s-agent.service.env; set +a; curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=<version> sh -'
```

**After each node:** `kubectl get nodes` shows the new version and `Ready`;
`kubectl get node <n> -o jsonpath='{.metadata.annotations.flannel\.alpha\.coreos\.com/public-ip}'`
equals the node's LAN IP (proves `flannel-iface` survived); no pod is stuck
(`kubectl get pods -A | grep -v -E "Running|Completed"`).

**After the master:** K3s re-applies its bundled add-ons. The Traefik
`helm-install-traefik` Job can fail once with `Required CRDs are missing`,
because it ran before the `traefik-crd` Job finished. Nothing is down (the
old Traefik keeps serving) and it succeeds on a retry; confirm with
`helm list -n kube-system`, `kubectl rollout status deploy/traefik -n
kube-system`, and a curl against the ingress hosts.

**Rollback:** stop K3s, restore `$BK/db` and `$BK/token` into
`/var/lib/rancher/k3s/server/`, put `$BK/k3s-old` back at
`/usr/local/bin/k3s`, start. Agents (stateless) are simply re-installed with
the old `INSTALL_K3S_VERSION`.

## Vaultwarden unreachable or burning its error budget

**Alerts:** `VaultwardenErrorBudgetBurnFast` (critical), `VaultwardenErrorBudgetBurnSlow` (warning), `VaultwardenProbeMissing` (critical). Source: the blackbox probe in `k8s/monitoring/vaultwarden-probe.yaml` and the SLO rules in `k8s/monitoring/vaultwarden-slo.yaml`.

The probe checks what a LAN or Tailscale client sees, so work through the path from the outside in:

1. Is the probe itself healthy? `probe_success{job="vaultwarden"}` in Grafana. If it is missing, check the blackbox pod (`kubectl logs -n monitoring deploy/blackbox-prometheus-blackbox-exporter`) and the Prometheus target page. That is a monitoring problem, not a Vaultwarden one.
2. Is Traefik answering? `curl -sk -o /dev/null -w "%{http_code}\n" --resolve vault.analitykbiznesowy.pl:443:192.168.50.50 https://vault.analitykbiznesowy.pl/`. Expect `200`. In the RED dashboard, check whether the errors are 5xx (backend) or the request rate dropped to zero (routing).
3. Is the backend up? `kubectl get pods -n vaultwarden` and `kubectl describe pod` for restarts and probe failures. Check whether the pod is on `g3-worker3`. If the node is off (dashboard shutdown), Vaultwarden is down by design; see the g3 shutdown runbook above.
4. Is the certificate still valid? `probe_ssl_earliest_cert_expiry` in Grafana. Renewal is manual; see the Vaultwarden TLS runbook above.
5. Clients that reach it only over Tailscale: check the tailnet is connected on the device. Without it the request goes to public DNS and gets the Hostido placeholder page, which the probe does not see.

After the fix, the slow and fast burn alerts clear on their own once the 5-minute and 30-minute windows recover. Record the outage in `docs/troubleshooting.md`, and check the monthly SLO panel in the Traefik RED dashboard.

## TrueNAS pool filling up

**Alerts:** `TrueNasPoolFillingUp` (warning, forecast within 4 days), `TrueNasPoolAlmostFull` (critical, above 85%). Source: node-exporter on TrueNAS, `k8s/monitoring/capacity-alerts.yaml`.

1. Find the dataset that grew: TrueNAS UI (Storage, dataset usage) or `zfs list -o name,used,avail -s used` on TrueNAS.
2. Snapshots keep deleted data. Check snapshot space before deleting files: `zfs list -t snapshot -o name,used -s used | tail`.
3. Downloads and media are the usual growth. Check qBittorrent's completed-download folder and the arr media folders before deleting anything still seeded or indexed.
4. Only after a clear cause: delete or move data, then confirm the forecast in Grafana (HDD activity dashboard, free space panel) moves back above the threshold.

## Vaultwarden backup

**What:** nightly CronJob `vaultwarden-backup` (03:30 Europe/Warsaw, runs on `g3-worker3` where the data volume lives). It takes a consistent SQLite copy with the online backup API, checks it with `integrity_check`, archives it with `rsa_key.pem` and attachments, encrypts it with **age** and uploads it to the MinIO bucket `backups` under `vaultwarden/`. Objects older than 14 days are pruned. Source: `k8s/vaultwarden/backup/`.

**Encryption:** only the public key is in the cluster. The private key is `~/.config/homelab-backup/age.key` on the admin workstation, with an offline copy kept outside the devcontainer. Without it, the backups cannot be read at all.

**Alerts:** `VaultwardenBackupFailed` (critical), `VaultwardenBackupStale` (no success in 26 h, critical), `VaultwardenBackupNeverRan` (warning).

**Check the last run:**
```
kubectl get cronjob,job -n vaultwarden
kubectl logs -n vaultwarden job/<last-job-name>
```
A healthy run ends with `ok key=vaultwarden/... bytes=...`.

**Run it now:**
```
kubectl create job --from=cronjob/vaultwarden-backup vaultwarden-backup-manual -n vaultwarden
```

**Restore (tested 2026-10-04 on a copy of the nightly backup, `db.sqlite3` passed `PRAGMA integrity_check`):**
1. Download the object from the `backups` bucket (`vaultwarden/vaultwarden-<stamp>.tar.gz.age`), for example with boto3 through a port-forward to `svc/minio` 9000.
2. Decrypt and unpack on the admin workstation:
   ```
   age -d -i ~/.config/homelab-backup/age.key -o restore.tar.gz vaultwarden-<stamp>.tar.gz.age && tar -xzf restore.tar.gz
   ```
3. Check the database: `python3 -c "import sqlite3; print(sqlite3.connect('db.sqlite3').execute('PRAGMA integrity_check').fetchone())"` must print `('ok',)`.
4. To restore into the cluster: scale `vaultwarden` to 0, copy `db.sqlite3`, `rsa_key.pem` and `attachments/` into the data volume, scale back to 1. Delete the stale `db.sqlite3-wal` and `-shm` files first.

**Credentials:** the secret `vaultwarden-backup-s3` holds the access key of the dedicated MinIO user `vaultwarden-backup`, limited by policy `backups-rw` to the `backups` bucket. Root credentials are not used by the backup. Rotate by creating a new service account for that user with `mc admin user svcacct add` and replacing the secret.

**Second copy (3-2-1):** the same encrypted object is written to the TrueNAS SMB share `vaultwarden-backups` (dataset `tank-bulk/vaultwarden-backups`, subfolder `vaultwarden/`, via PVC `vaultwarden-backups`). Its credentials are the TrueNAS user `vwbackup` in Secret `smb-vaultwarden-backups-secret` (kube-system). Lessons from setting it up: the share's **Hosts Allow** field takes IP addresses only (a username there blocks everyone), and user access is set in **Edit Share ACL**, not in the share's edit form.

**Restore from the TrueNAS copy (tested 2026-10-04):** the same object is on the share `vaultwarden-backups`, folder `vaultwarden/`. Download it with `smbclient //192.168.50.21/vaultwarden-backups -U vwbackup -c 'cd vaultwarden; get <file> restore.age'` (password typed at the prompt), then decrypt and check it exactly as in step 2 and 3 above. Remove the decrypted files afterwards, because they contain `rsa_key.pem`.

**Not done yet:** scheduled verification. Restores are only tested by hand, so a backup that silently stopped being decryptable would be found late. A quarterly restore drill is the next improvement.

## Pi-hole DNS unreachable or wrong answers

**Alerts:** `PiholeDnsBurnFast` (critical), `PiholeDnsBurnSlow` (warning), `PiholeDnsProbeMissing` (critical), `PiholeUpstreamDnsFailing` (warning). Source: DNS probes in `k8s/monitoring/pihole-dns-probes.yaml`, SLO in `k8s/monitoring/pihole-dns-slo.yaml`.

The internal probe asks Pi-hole for `vault.home.local` and expects `192.168.50.50`. A failure means the local override is missing or wrong, which breaks every internal service. The upstream probe asks for `example.com`, so a failure means Pi-hole cannot forward to its upstream resolvers.

1. Is the probe itself healthy? Check the target in Prometheus (`probe_success{job="pihole-dns-internal"}`). If it is missing, check the blackbox pod before blaming Pi-hole.
2. Ask Pi-hole directly from a LAN machine: `dig @192.168.50.53 vault.home.local +short` must print `192.168.50.50`, and `dig @192.168.50.53 example.com +short` must print addresses.
3. If the local name is wrong, check the custom DNS entries in the Pi-hole web UI (or the `pihole` pod's custom list). Restoring the correct entry is what `automation-practice/pihole_entry.sh` does, idempotently.
4. If upstream fails and local names work, check the upstream resolvers Pi-hole is configured with, and whether the router (192.168.50.1) is blocking outbound DNS.
5. Check the pod: `kubectl get pods -n pihole` and `kubectl describe pod -n pihole <pod>` for restarts. Clients fall back to the router's DNS when Pi-hole is down, so symptoms can be intermittent.

After the fix, the burn-rate alerts clear once the 5-minute and 30-minute windows recover. Record the incident in `docs/troubleshooting.md`.
