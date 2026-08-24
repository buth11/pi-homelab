# pi-homelab — Homelab K3s Cluster

## Role & Context

You are acting as a **Senior DevOps/SRE Engineer** responsible for
maintaining and evolving this environment: a single-operator homelab
running K3s on mixed Raspberry Pi / mini-PC hardware, with Proxmox VE +
TrueNAS SCALE as the virtualization/storage tier, Home Assistant/Zigbee
for smart-home, and a mix of Terraform/Ansible/Helm/ArgoCD for
infrastructure-as-code. Treat this like production for a team of one:
changes should be safe to make at 11pm without a second reviewer, which
means favoring reversible steps, checking live cluster state before
assuming the repo reflects reality, and writing down *why* when something
non-obvious gets fixed (see [docs/troubleshooting.md](docs/troubleshooting.md)
and [docs/postmortems/](docs/postmortems/) — this is a real, actively-used
incident log, not decoration).

## Project Overview

K3s cluster (4 nodes: 1 control-plane, 3 workers — 2 physical Pi/mini-PC,
1 Proxmox VM) backed by a TrueNAS SCALE ZFS storage tier reached over a
dedicated 2.5GbE link, not the LAN. Services run as one namespace per
app, mostly plain `kubectl apply`, with ArgoCD managing exactly one app
(`arr-stack`) so far as a GitOps pilot. Home Assistant + Zigbee2MQTT run
as a Proxmox VM alongside the cluster, not inside it. See
[docs/KUBERNETES.md](docs/KUBERNETES.md) and
[docs/INFRASTRUCTURE.md](docs/INFRASTRUCTURE.md) for the full picture —
this file is the fast-lookup summary, not the source of truth for detail.

## Documentation Map

| Doc | Covers |
|---|---|
| [docs/KUBERNETES.md](docs/KUBERNETES.md) | K3s conventions: namespaces, GitOps status, StorageClasses, Ingress/TLS, secret-handling mechanics, common `kubectl` commands |
| [docs/INFRASTRUCTURE.md](docs/INFRASTRUCTURE.md) | Proxmox VMs, TrueNAS pools/snapshots, Home Assistant/Zigbee — operational reference |
| [docs/architecture.md](docs/architecture.md) | Full design rationale behind the storage/network layout (the "why", not just the "what") |
| [docs/SECURITY.md](docs/SECURITY.md) | Credential-handling policy, RBAC principles, network segmentation status |
| [docs/security/](docs/security/) | Point-in-time audits and the external pentest report |
| [docs/RUNBOOKS.md](docs/RUNBOOKS.md) | Prescriptive "if X happens, do Y" procedures |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Chronological incident log — what actually went wrong and how it was diagnosed |
| [docs/postmortems/](docs/postmortems/) | Formal per-incident postmortems (template included) |
| [docs/usefull_commands.md](docs/usefull_commands.md) | Extended command reference |
| [setup/](setup/) | Numbered "how this was built" walkthroughs, one per major deployment |

## Standards & Conventions

- **Commits:** Conventional Commits, consistently — `feat(scope): ...`,
  `fix(scope): ...`, `docs(scope): ...`, `chore: ...`. A commit that
  removes or rotates a leaked credential should say so explicitly in the
  subject and confirm rotation happened, not just file removal (see
  [docs/SECURITY.md](docs/SECURITY.md)).
- **Secrets:** never a literal credential in a committed file. Terraform
  uses `sensitive = true` vars, Helm uses `REPLACE_ME`-style placeholders
  filled in via `--set`, raw manifests use a `_PLACEHOLDER` literal
  filled in only on the live cluster. Full pattern and rationale in
  [docs/SECURITY.md](docs/SECURITY.md) and
  [docs/KUBERNETES.md](docs/KUBERNETES.md#secret-handling-the-pattern-actually-in-use).
- **Manifests:** one namespace per application; `local-path` PVCs always
  paired with a matching `nodeSelector`; SQLite-backed apps never on SMB
  (CIFS doesn't support the locking WAL mode needs — this has caused a
  real incident, don't repeat it). RBAC scoped to a namespaced `Role`
  unless cross-namespace access is genuinely required.
- **Before hand-editing anything ArgoCD manages** (currently `k8s/arr/`),
  check `kubectl get application -n argocd` — `selfHeal: true` will
  revert an out-of-band change.
- **IaC tools in use:** Terraform (`terraform/`, S3-compatible backend on
  in-cluster MinIO), Ansible (`ansible/`, node-level config/hardening),
  Helm (`helm/`, monitoring stack), ArgoCD (`argocd/`, one app so far),
  plain `kubectl apply` everywhere else.

## Hardware

- **pi4-master**: 192.168.50.10, Pi 4B 4GB, control-plane
- **pi4-worker2**: 192.168.50.12, Pi 4B 4GB, worker
- **g3-worker3**: 192.168.50.13, GMKtec G3 Mini (Intel N100, 16GB), media workloads, Ubuntu Server
- **k3s-burst-worker**: 192.168.50.30, Proxmox VM worker (runs one K3s minor version ahead of the rest — see docs/KUBERNETES.md)
- **Proxmox VE**: 192.168.50.20 (mgmt) / 10.10.10.1 (storage net), GMKtec Topton FU02 AMD 7730U, 64GB RAM, 1TB SATA SSD + 2TB NVMe local
- **TrueNAS SCALE**: 192.168.50.21 (mgmt) / 10.10.10.2 (storage net), GMKtec Topton AMD 5600U, 29GB RAM, 3 ZFS pools (~12.5 TiB usable)
- **NAS (legacy)**: //ASUS/Crucial_2TB (SMB, 2TB, guest access) — media library migrated off this to TrueNAS, kept for qBittorrent working dir
- **Router**: ASUS RT-AX86U, 192.168.50.1

## Network / IP Layout

- 192.168.50.50 - Traefik Ingress LoadBalancer (vault.home.local / vault.analitykbiznesowy.pl)
- 192.168.50.51 - File Browser :8080 (NAS file browser)
- 192.168.50.53 - Pi-hole DNS
- 192.168.50.54 - qBittorrent WebUI :8080
- 192.168.50.55 - Firefox noVNC :3001
- 192.168.50.56 - Jellyfin :8096
- 192.168.50.57 - Pi-hole Web Admin
- 192.168.50.58 - Homelab Dashboard
- MetalLB pool: 192.168.50.50-80

## Services

- **Pi-hole**: namespace pihole, DNS ad-blocking
- **qBittorrent**: namespace qbittorrent, ProtonVPN Plus WireGuard
- **Firefox**: namespace qbittorrent, noVNC browser
- **Jellyfin**: namespace jellyfin, media server, Intel QSV hardware acceleration
- **Vaultwarden**: namespace vaultwarden, self-hosted password manager, Traefik Ingress at vault.home.local + vault.analitykbiznesowy.pl (real Let's Encrypt cert via Hostido AutoSSL, split-DNS only via Pi-hole + Tailscale — see setup/07-vaultwarden-tls-hostido.md)
- **arr-stack** (Prowlarr/Sonarr/Radarr): namespace arr, on g3-worker3, GitOps-managed via ArgoCD
- **Homelab Dashboard**: namespace dashboard, cluster control/WoL UI — hardening tracked internally, see docs/SECURITY.md#authentication-on-internal-services
- **File Browser**: namespace filebrowser, NAS file browser
- **MinIO**: namespace minio, S3-compatible storage (also serves as the Terraform state backend)
- **Uptime Kuma**: namespace uptime-kuma, status monitoring
- **Prometheus/Grafana/Alertmanager**: namespace monitoring, Helm-managed (`helm/node-alerting`)
- **MetalLB**: IP pool 192.168.50.50-80
- **SMB CSI Driver**: StorageClass nas-smb -> //ASUS/Crucial_2TB

## VPN

- ProtonVPN Plus P2P WireGuard na g3-worker3
- Config: /etc/wireguard/proton.conf
- Auto-start: wg-quick@proton.service
- IP: 205.147.16.83 (Netherlands P2P)
- Tailscale: k8s-operator + subnet router, advertises 192.168.50.0/24 to the tailnet (k8s/tailscale/connector.yaml)

## TLS Certificates

- vaultwarden-tls (namespace vaultwarden): real Let's Encrypt cert for vault.home.local + vault.analitykbiznesowy.pl, issued manually via Hostido DirectAdmin AutoSSL panel — expires **2026-11-22**, renewal is MANUAL (no ACME client on the cluster). Procedure: docs/RUNBOOKS.md#vaultwarden-tls-certificate-approaching-expiry

## CronJobs

- shutdown-pods: 22:55 - scale down qbittorrent i jellyfin
- shutdown-g3: 23:00 - SSH shutdown g3-worker3

## Storage (K3s StorageClasses)

Quick reference — full detail in [docs/KUBERNETES.md](docs/KUBERNETES.md#storageclasses).
- local-path: domyślny dla config PVC (node-local, no snapshots)
- nas-smb: SMB CSI -> //ASUS/Crucial_2TB (legacy, qBittorrent working dir only)
- smb-tank-fast: SMB CSI -> TrueNAS tank-fast/k3s-pv (ZFS-snapshotted)
- smb-tank-bulk-media: SMB CSI -> TrueNAS tank-bulk/media (Jellyfin, reclaimPolicy Retain)

## Repository

- github.com/buth11/pi-homelab
- Devcontainer: .devcontainer/devcontainer.json

## Key Commands

```bash
# Start/Stop services
kubectl scale deployment qbittorrent -n qbittorrent --replicas=1
kubectl scale deployment jellyfin -n jellyfin --replicas=1

# Check cluster
kubectl get nodes
kubectl get pods -A
kubectl get application -n argocd    # what's under GitOps

# qBittorrent password
kubectl logs -n qbittorrent $(kubectl get pods -n qbittorrent -o name | head -1) -c qbittorrent | grep -i password

# Shutdown G3
ssh buth11@192.168.50.13 "sudo shutdown -h now"
```
