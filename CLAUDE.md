cat > /workspaces/pi-homelab/CLAUDE.md << 'EOF'
# pi-homelab - Homelab K3s Cluster

## Hardware
- **pi4-master**: 192.168.50.10, Pi 4B 4GB, control-plane
- **pi4-worker2**: 192.168.50.12, Pi 4B 4GB, worker
- **g3-worker3**: 192.168.50.13, GMKtec G3 Mini (Intel N100, 16GB), media workloads, Ubuntu Server
- **Proxmox VE**: 192.168.50.20 (mgmt) / 10.10.10.1 (storage net), GMKtec Topton FU02 AMD 7730U, 64GB RAM, 1TB SATA SSD + 2TB NVMe local
- **TrueNAS SCALE**: 192.168.50.21 (mgmt) / 10.10.10.2 (storage net), GMKtec Topton AMD 5600U, 29GB RAM, 3 ZFS pools (~12.5 TiB usable)
- **NAS (legacy)**: //ASUS/Crucial_2TB (SMB, 2TB, guest access) — media library migrated off this to TrueNAS, kept for qBittorrent working dir
- **Router**: ASUS RT-AX86U, 192.168.50.1

## Proxmox / TrueNAS storage
See `docs/architecture.md` for full design rationale. Summary:
- **tank-fast** (~903 GiB, all-SSD mirrors): Proxmox VM disks (NFS) + K3s PVs (SMB)
- **tank-bulk** (~10.8 TiB, HDD mirrors + SLOG/L2ARC on spare SSDs): Jellyfin media + backups
- **scratch-nvme** (~899 GiB, single NVMe, no redundancy): scratch/working data
- NFS storage `tank-fast-nfs` in Proxmox -> `10.10.10.2:/mnt/tank-fast/vm-storage` (dedicated storage-net link, not LAN)
- Periodic ZFS snapshots: vm-storage/k3s-pv @ 10:00/15:00/20:00 daily (2wk retention), media @ 10:00 daily (1wk), backups @ 10:00 daily (4wk)

## Home Assistant / Zigbee (Proxmox VM 100)
- HAOS 18.1, 2vCPU/4GB, disk on tank-fast-nfs, OVMF/UEFI
- Zigbee USB coordinator passthrough by vendor:product ID (`1a86:7523`), NOT by physical port
- Zigbee2MQTT adapter = `zstack` (NOT `ember` despite dongle's multiprotocol marketing — ember fails ASH handshake)
- Mosquitto (`core-mosquitto`) + Zigbee2MQTT add-ons, MQTT integration auto-discovers devices
- 19 Zigbee devices / 200 HA entities (8x Nous A7Z + 4x Girier JR-ZPM01 metering plugs as mesh routers, 4x HOBEIAN temp/humidity, 1x Aqara weather, 1x Tuya air quality)

## Network / IP Layout
- 192.168.50.53 - Pi-hole DNS
- 192.168.50.54 - qBittorrent WebUI :8080
- 192.168.50.55 - Firefox noVNC :3001
- 192.168.50.56 - Jellyfin :8096
- 192.168.50.57 - Pi-hole Web Admin

## Services
- **Pi-hole**: namespace pihole, DNS ad-blocking
- **qBittorrent**: namespace qbittorrent, ProtonVPN Plus WireGuard
- **Firefox**: namespace qbittorrent, noVNC browser
- **Jellyfin**: namespace jellyfin, media server, Intel QSV hardware acceleration
- **Prometheus/Grafana**: namespace monitoring
- **MetalLB**: IP pool 192.168.50.50-60
- **SMB CSI Driver**: StorageClass nas-smb -> //ASUS/Crucial_2TB

## VPN
- ProtonVPN Plus P2P WireGuard na g3-worker3
- Config: /etc/wireguard/proton.conf
- Auto-start: wg-quick@proton.service
- IP: 205.147.16.83 (Netherlands P2P)

## CronJobs
- shutdown-pods: 22:55 - scale down qbittorrent i jellyfin
- shutdown-g3: 23:00 - SSH shutdown g3-worker3

## Storage (K3s StorageClasses)
- local-path: domyślny dla config PVC
- nas-smb: SMB CSI -> //ASUS/Crucial_2TB (legacy, qBittorrent working dir only)
- smb-tank-fast: SMB CSI -> TrueNAS tank-fast/k3s-pv
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

# qBittorrent password
kubectl logs -n qbittorrent $(kubectl get pods -n qbittorrent -o name | head -1) -c qbittorrent | grep -i password

# Shutdown G3
ssh buth11@192.168.50.13 "sudo shutdown -h now"
```
EOF

git add .
git commit -m "docs: add CLAUDE.md for Claude Code context"
git push
## Dodatkowe serwisy
- **File Browser**: 192.168.50.51:8080 — przeglądarka plików NAS
