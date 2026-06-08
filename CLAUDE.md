cat > /workspaces/pi-homelab/CLAUDE.md << 'EOF'
# pi-homelab - Homelab K3s Cluster

## Hardware
- **pi4-master**: 192.168.50.10, Pi 4B 4GB, control-plane
- **pi4-worker2**: 192.168.50.12, Pi 4B 4GB, worker
- **g3-worker3**: 192.168.50.13, GMKtec G3 Mini (Intel N100, 16GB), media workloads, Ubuntu Server
- **NAS**: //ASUS/Crucial_2TB (SMB, 2TB, guest access)
- **Router**: ASUS RT-AX86U, 192.168.50.1

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

## Storage
- local-path: domyślny dla config PVC
- nas-smb: SMB CSI -> NAS dla downloads i media

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
