# Kubernetes (K3s) — Conventions & Reference

> How this cluster is actually built and operated day to day. For *why*
> the storage/network layers look the way they do, see
> [architecture.md](architecture.md); for incident history see
> [troubleshooting.md](troubleshooting.md); for credential/RBAC policy see
> [SECURITY.md](SECURITY.md).

## Cluster topology

| Node | Role | K3s version | OS |
|------|------|-------------|-----|
| pi4-master | control-plane | v1.35.5+k3s1 | Debian 12 (bookworm) |
| pi4-worker2 | worker | v1.35.5+k3s1 | Debian 12 (bookworm) |
| g3-worker3 | worker, media/QSV | v1.35.5+k3s1 | Ubuntu 24.04.4 LTS |
| k3s-burst-worker | worker (Proxmox VM) | v1.36.2+k3s1 | Ubuntu 24.04.4 LTS |

`k3s-burst-worker` runs one minor version ahead of the rest — known, not
yet reconciled; check this table against `kubectl get nodes` before
assuming version parity when debugging something version-sensitive.

## Namespace map

One namespace per application (`pihole`, `qbittorrent`, `jellyfin`,
`vaultwarden`, `arr`, `dashboard`, `monitoring`, `minio`, `uptime-kuma`,
`argocd`), plus `kube-system` for cluster-scoped Secrets that CSI drivers
need (e.g. `smb-tank-fast-secret`). Don't collapse unrelated apps into a
shared namespace — see the rationale already written up in
[setup/06-arr-stack.md](../setup/06-arr-stack.md#why-a-separate-namespace).

## GitOps status: partial, in migration

**ArgoCD is live** (see `argocd/applications/arr-stack.yaml`) and manages
exactly one app so far: `arr` (Prowlarr/Sonarr/Radarr), synced from
`k8s/arr/` with `prune: true` + `selfHeal: true` — meaning that directory
*is* the live source of truth; a manual `kubectl edit` against anything in
the `arr` namespace will be reverted automatically.

**Everything else is still manually applied** (`kubectl apply -f
k8s/<app>/`, or a `deploy.sh` script for `dashboard/`). The README
roadmap lists FluxCD as a future step — in practice ArgoCD is what's
actually installed and running today; treat the roadmap entry as stale
until the rest of the cluster is migrated onto GitOps (either finish the
ArgoCD rollout or reconcile the roadmap to match).

**Before hand-editing anything under `k8s/arr/`**, check
`kubectl get application arr-stack -n argocd` first — it will self-heal
over an out-of-band edit within its sync interval.

## StorageClasses

| StorageClass | Backing | Use |
|---|---|---|
| `local-path` | node-local `hostPath` (K3s built-in) | default for config PVCs; **no replication, no snapshots** — see [SECURITY.md](SECURITY.md) for the Pi-hole finding this produced |
| `nas-smb` | SMB → `//ASUS/Crucial_2TB` | legacy; qBittorrent working dir only |
| `smb-tank-fast` | SMB CSI → TrueNAS `tank-fast/k3s-pv` | general K3s PVs, ZFS-snapshotted |
| `smb-tank-bulk-media` | SMB CSI → TrueNAS `tank-bulk/media` | Jellyfin library, `reclaimPolicy: Retain` |

`local-path` PVs are pinned via `nodeAffinity` to whichever node first
claimed them — always pair a `local-path` PVC with a matching
`nodeSelector` on the Deployment (see `k8s/pihole/deployment.yaml` for
the pattern), or the pod will fail to schedule after any node change.

SQLite-backed apps (Prowlarr/Sonarr/Radarr, Syncthing) **must** use
`local-path`, never SMB — CIFS doesn't support the file locking SQLite's
WAL mode needs. This produced a real incident
([troubleshooting.md, 2026-07-25](troubleshooting.md#2026-07-25----qbittorrent-port-drift-syncthing-node-migration-cifs-mojibake-and-stale-dentry-cache));
don't repeat it.

## Ingress / Traefik conventions

Every Ingress uses:
```yaml
annotations:
  traefik.ingress.kubernetes.io/router.entrypoints: websecure
```
to force HTTPS-only routing (see `k8s/vaultwarden/ingress.yaml`). TLS is
**not** automated — there is no cert-manager in this cluster. Certificates
are either `mkcert` (LAN-only, self-signed, trusted only on devices with
the local CA installed) or, for Vaultwarden, a real Let's Encrypt cert
issued manually through Hostido's AutoSSL panel and applied by hand — see
[setup/07-vaultwarden-tls-hostido.md](../setup/07-vaultwarden-tls-hostido.md).
If a service needs a real trusted cert with no public DNS record, that
setup doc is the template to follow (split-DNS via Pi-hole + Tailscale,
not a live ACME client).

## Secret handling: the pattern actually in use

There's no SealedSecrets / External Secrets / Vault in this cluster.
What's used instead, consistently, everywhere it's done right:
- **Terraform:** `sensitive = true` variables, values supplied outside
  the repo at `apply` time — never literal in `.tf`.
- **Helm:** `REPLACE_ME` / templated placeholders in `values.yaml`, real
  value passed via `--set` at install/upgrade time.
- **Raw K8s manifests:** a `_PLACEHOLDER` literal in `stringData`/`data`,
  filled in only on the live cluster via `kubectl patch` or
  `kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -`
  (this exact pattern is in
  [setup/07-vaultwarden-tls-hostido.md](../setup/07-vaultwarden-tls-hostido.md)),
  never edited in place and committed.

Follow whichever of these matches the resource type you're touching. See
[SECURITY.md](SECURITY.md) for the policy this pattern exists to enforce,
and for what's gone wrong when it wasn't followed.

## Common commands

```bash
# Cluster state
kubectl get nodes
kubectl get pods -A
kubectl get application -n argocd          # what's under GitOps

# Scale a service
kubectl scale deployment <name> -n <namespace> --replicas=1

# StorageClass / PVC sanity check before touching local-path apps
kubectl get pvc -n <namespace> -o wide
kubectl get pv -o custom-columns=NAME:.metadata.name,NODE:.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values

# Secret rollout without writing the rendered manifest to disk
kubectl create secret generic <name> -n <namespace> \
  --from-literal=key=value \
  --dry-run=client -o yaml | kubectl apply -f -
```
