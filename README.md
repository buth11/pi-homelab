# Pi Homelab — K3s Bare-Metal Cluster

> Personal SRE/DevOps homelab running K3s on Raspberry Pi hardware.
> Built to learn, break, fix, and document production-like infrastructure.

## Architecture
Internet
│
ASUS RT-AX86U Router (192.168.50.1)
│
├── K3s Cluster (192.168.50.0/24)
│     MASTER:  pi4-master   (192.168.50.10)
│     WORKER:  pi4-worker2  (192.168.50.12)
│     WORKER:  g3-worker3   (192.168.50.13 — GMKtec N100, QSV transcoding)
│
├── Proxmox VE (192.168.50.20) ── VM: Home Assistant OS
│
└── TrueNAS SCALE (192.168.50.21) ── ZFS storage backend for both Proxmox and K3s
      dedicated 2.5GbE storage link: Proxmox ⇄ TrueNAS (10.10.10.0/30)

See [docs/architecture.md](docs/architecture.md) for full storage pool design,
snapshot policy, network segmentation, and the Home Assistant/Zigbee subsystem.

## Stack

| Layer | Technology |
|-------|-----------|
| Container orchestration | K3s (Kubernetes) |
| Hypervisor | Proxmox VE |
| Storage | TrueNAS SCALE (ZFS, NFS + SMB) |
| Networking | Flannel CNI, MetalLB, Traefik |
| DNS / Ad-blocking | Pi-hole |
| VPN mesh | Tailscale |
| Observability | Prometheus, Grafana, Loki |
| GitOps | FluxCD |
| Smart home | Home Assistant, Zigbee2MQTT, Mosquitto |
| AI Agents | OpenHands (Hermes stack) |

## Nodes

| Hostname | Model | Role | RAM | Storage |
|----------|-------|------|-----|---------|
| pi4-master | Raspberry Pi 4 | K3s control-plane | 4GB | — |
| pi4-worker2 | Raspberry Pi 4 | K3s worker | 4GB | — |
| g3-worker3 | GMKtec G3 Mini (Intel N100) | K3s worker, media transcoding | 16GB | — |
| Proxmox host | GMKtec Topton FU02 (AMD 7730U) | Hypervisor | 64GB | 1TB SATA SSD + 2TB NVMe (local) |
| TrueNAS host | GMKtec Topton (AMD 5600U) | ZFS storage backend | 29GB | 3 pools, ~12.5 TiB usable — see [architecture.md](docs/architecture.md) |

## Roadmap

- [x] Hardware setup
- [x] OS installation (Raspberry Pi OS Lite 64-bit Bookworm)
- [x] Node preparation (cgroups, swap disabled)
- [x] K3s installation — 3 nodes Ready
- [x] MetalLB + Traefik Ingress
- [x] Pi-hole deployment
- [x] Jellyfin + qBittorrent (WireGuard VPN) + SMB CSI storage
- [x] Proxmox + TrueNAS storage tier (ZFS pools, NFS/SMB integration)
- [x] Dedicated storage network (Proxmox ⇄ TrueNAS, 2.5GbE)
- [x] Periodic ZFS snapshots (VM disks, K3s PVs, media, backups)
- [x] Home Assistant + Zigbee2MQTT smart-home subsystem
- [ ] Tailscale operator
- [ ] Prometheus + Grafana + Loki
- [ ] FluxCD GitOps
- [ ] Hermes agents migration

## Progress Journal

### 2026-05-29
- Flashed OS on all three nodes
- Configured static IP reservations on router
- Disabled swap, enabled cgroups on all nodes
- Created this repository

### 2026-07-11
- Added Proxmox VE (192.168.50.20) and TrueNAS SCALE (192.168.50.21) as a
  dedicated hypervisor/storage tier
- Designed and provisioned 3 ZFS pools on TrueNAS (`tank-fast`, `tank-bulk`,
  `scratch-nvme`) with role-based SLOG/L2ARC placement — see
  [docs/architecture.md](docs/architecture.md) §3
- Wired NFS (Proxmox VM storage) and SMB (K3s PV + media) integration between
  TrueNAS and both the hypervisor and the cluster
- Migrated the Jellyfin media library (562GB, 343 files) from the ASUS
  router's SMB share to `tank-bulk/media`; re-pointed Jellyfin/FileBrowser
  PVCs with zero data loss
- Configured a dedicated point-to-point 2.5GbE storage network
  (10.10.10.0/30) between Proxmox and TrueNAS, migrated the NFS mount to it
  with zero VM downtime
- Set up periodic ZFS snapshots across all persistent datasets
- Deployed Home Assistant OS 18.1 as a Proxmox VM with USB Zigbee coordinator
  passthrough; brought up Zigbee2MQTT + Mosquitto and paired 19 Zigbee
  devices (metering smart plugs + temperature/humidity/air-quality sensors)

