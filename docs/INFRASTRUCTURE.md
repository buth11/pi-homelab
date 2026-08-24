# Infrastructure — Proxmox, TrueNAS, Home Assistant/Zigbee

> Operational reference: what's running, where, and how to touch it
> safely. For full design rationale (why these ZFS pools, why this
> network segmentation) see [architecture.md](architecture.md) — this
> doc is the shorter "what do I actually do" companion to it.

## Compute layer

| Host | Role | Mgmt IP | Storage-net IP | Spec |
|---|---|---|---|---|
| Proxmox VE | Hypervisor | 192.168.50.20 | 10.10.10.1 | GMKtec Topton FU02, AMD 7730U, 64GB RAM, 1TB SATA SSD + 2TB NVMe local |
| TrueNAS SCALE | ZFS storage backend | 192.168.50.21 | 10.10.10.2 | GMKtec Topton, AMD 5600U, 29GB RAM, 3 ZFS pools (~12.5 TiB usable) |

Proxmox and TrueNAS talk over a **dedicated point-to-point 2.5GbE link**
(`10.10.10.0/30`), not the LAN — NFS (VM disks) and the SMB CSI driver's
node-stage traffic both ride this link. Never point a new storage
integration at the `.50.x` management IPs when a `10.10.10.x` path is
available; that defeats the point of the dedicated link (see
[architecture.md §5](architecture.md#5-dedicated-storage-network)).

## Proxmox VMs

| VM | Purpose | Storage |
|---|---|---|
| Home Assistant OS (HAOS 18.1) | Zigbee2MQTT + smart-home hub | `tank-fast-nfs`, OVMF/UEFI |
| `k3s-burst-worker` | K3s worker node | `tank-fast-nfs` |

Both VM disks live on `tank-fast-nfs` (`10.10.10.2:/mnt/tank-fast/vm-storage`),
covered by the same periodic ZFS snapshot policy as K3s PVs (see
[Storage & snapshots](#storage--snapshots) below).

## Storage & snapshots

Three pools on TrueNAS, role-separated by workload:

| Pool | Size | Layout | Used for |
|---|---|---|---|
| `tank-fast` | ~903 GiB | all-SSD mirrors | Proxmox VM disks (NFS), K3s PVs (SMB) |
| `tank-bulk` | ~10.8 TiB | HDD mirrors + SLOG/L2ARC on spare SSDs | Jellyfin media, backups |
| `scratch-nvme` | ~899 GiB | single NVMe, no redundancy | scratch/working data — **treat as disposable** |

Snapshot schedule:

| Dataset | Frequency | Retention |
|---|---|---|
| `vm-storage`/`k3s-pv` | 10:00 / 15:00 / 20:00 daily | 2 weeks |
| `media` | 10:00 daily | 1 week |
| `backups` | 10:00 daily | 4 weeks |

**`local-path` K3s PVs are outside this entirely** — they're node-local
`hostPath` volumes with no snapshot coverage at all. If something needs
snapshot protection, it needs to be on `smb-tank-fast` or
`smb-tank-bulk-media`, not `local-path`. Pi-hole's config currently sits
on `local-path` for exactly this reason worth calling out.

## Home Assistant / Zigbee

- HAOS 18.1, 2 vCPU / 4GB, Proxmox VM 100, OVMF/UEFI
- Zigbee coordinator passthrough is by **USB vendor:product ID**
  (`1a86:7523`), not physical port — a port-based passthrough breaks the
  first time the dongle is unplugged/replugged into a different port.
- Zigbee2MQTT adapter setting **must be `zstack`**, not `ember` — despite
  the dongle's multiprotocol marketing, `ember` fails the ASH handshake
  (`HOST_FATAL_ERROR`) on first connect. Set this explicitly via the
  onboarding wizard's dedicated Serial tab; the auto-populated main tab
  has been observed silently resetting the selection on page reload.
- Mosquitto (`core-mosquitto`) + Zigbee2MQTT add-ons; the MQTT
  integration auto-discovers devices, no manual entity config needed.
- 19 Zigbee devices / ~200 HA entities: 8× Nous A7Z + 4× Girier
  JR-ZPM01 metering plugs (also serve as mesh routers), 4× HOBEIAN
  temp/humidity, 1× Aqara weather, 1× Tuya air quality.

## Legacy NAS

`//ASUS/Crucial_2TB` (SMB, 2TB, guest access) — the media library has
been fully migrated off this to TrueNAS `tank-bulk`; the only thing still
pointed at it is qBittorrent's working/download directory (`nas-smb`
StorageClass). Don't add new PVCs against it; it's being kept alive for
exactly one legacy mount, not as ongoing shared storage.

## See also

- [architecture.md](architecture.md) — full design rationale, disk
  inventory, dataset layout, protocol integration detail
- [hardware.md](hardware.md) — physical hardware notes
- [KUBERNETES.md](KUBERNETES.md) — how the K3s layer that sits on top of
  this infrastructure is operated
