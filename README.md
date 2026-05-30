# Pi Homelab — K3s Bare-Metal Cluster

> Personal SRE/DevOps homelab running K3s on Raspberry Pi hardware.
> Built to learn, break, fix, and document production-like infrastructure.

## Architecture
Internet
│
TP-Link Router
│
┌─────────────────────────────────┐
│         K3s Cluster             │
│                                 │
│  MASTER:  Pi 3B+ (1GB RAM)      │
│  WORKER1: Pi 4  (xGB RAM)       │
│  WORKER2: Pi 4  (xGB RAM)       │
└─────────────────────────────────┘
│
GMKtec EVO-X2 (LM Studio — LLM backend)

## Stack

| Layer | Technology |
|-------|-----------|
| Container orchestration | K3s (Kubernetes) |
| Networking | Flannel CNI, MetalLB, Traefik |
| DNS / Ad-blocking | Pi-hole |
| VPN mesh | Tailscale |
| Observability | Prometheus, Grafana, Loki |
| GitOps | FluxCD |
| AI Agents | OpenHands (Hermes stack) |

## Nodes

| Hostname | Model | Role | RAM | Storage |
|----------|-------|------|-----|---------|
| pi3-master | Raspberry Pi 3B+ | K3s master | 1GB | 32GB SanDisk Ultra |
| pi4-worker1 | Raspberry Pi 4 | K3s worker | xGB | 128GB SanDisk Extreme |
| pi4-worker2 | Raspberry Pi 4 | K3s worker | xGB | 64GB Kingston |

## Roadmap

- [x] Hardware setup
- [x] OS installation (Raspberry Pi OS Lite 64-bit Bookworm)
- [x] Node preparation (cgroups, swap disabled)
- [x] K3s installation — 3 nodes Ready
- [ ] MetalLB + Traefik Ingress
- [ ] Pi-hole deployment
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

