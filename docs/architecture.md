# Architecture

> Last updated: 2026-07-12
> This document describes the infrastructure layers added on top of the original
> K3s Raspberry Pi cluster: a dedicated hypervisor/storage tier (Proxmox + TrueNAS),
> its integration with K3s and Proxmox, and a Home Assistant / Zigbee smart-home
> subsystem running on top of it.

## 1. Topology overview

```
Internet
   |
ASUS RT-AX86U Router (192.168.50.1)
   |
   |-- direct LAN ports (1GbE) -------------------------------
   |     |-- pi4-master     192.168.50.10
   |     `-- pi4-worker2    192.168.50.12
   |
   `-- 2.5GbE port -> TP-Link 2.5GbE switch ---------------------
         |-- g3-worker3     192.168.50.13  (Intel N100, QSV transcoding)
         |-- Proxmox VE     192.168.50.20  (AMD 7730U, 64GB RAM)
         |     `-- VM 100: Home Assistant OS 18.1 (4GB / 2vCPU)
         `-- TrueNAS SCALE  192.168.50.21  (AMD 5600U, 29GB RAM)

10.10.10.0/30 -- dedicated storage network (2.5GbE, direct-attach, separate cable)
   |-- Proxmox   nic0   10.10.10.1
   `-- TrueNAS   eno1   10.10.10.2
```

Physical layout: the ASUS router's single 2.5GbE port feeds a dedicated
TP-Link 2.5GbE switch, which is where the three "server-class" hosts
(g3-worker3, Proxmox, TrueNAS) all connect — keeping the highest-bandwidth
traffic on 2.5GbE hardware end-to-end rather than bottlenecking through the
router's other 1GbE ports. The two Raspberry Pi nodes, which don't benefit
from more than 1GbE, connect directly to the router's regular LAN ports.

On top of that shared 2.5GbE switch segment, Proxmox and TrueNAS also have
a second, *separate* direct-attach cable between them (10.10.10.0/30, §5)
used exclusively for NFS traffic — bypassing even the TP-Link switch for
the single highest-throughput, most latency-sensitive path in the network.

## 2. Compute layer -- Proxmox VE

| Item | Value |
|---|---|
| Host | GMKtec Topton FU02, AMD Ryzen 7730U |
| RAM | 64GB |
| Local disks | 1TB SATA SSD, 2TB NVMe (WD Black) |
| Management IP | 192.168.50.20 |
| Storage-net IP | 10.10.10.1/30 |

Currently hosts one VM (`home-assistant`, VMID 100). Local disks are reserved
for Proxmox's own OS and `local-lvm`; VM disk images are provisioned on
TrueNAS via NFS rather than local storage, so VMs are not tied to the
physical hypervisor's disks and can, in principle, be re-pointed at another
hypervisor without a storage migration.

## 3. Storage layer -- TrueNAS SCALE

| Item | Value |
|---|---|
| Host | GMKtec Topton, AMD Ryzen 5 5600U |
| RAM | 29.3GB usable |
| Version | TrueNAS SCALE 25.10.4 "Goldeye" |
| Management IP | 192.168.50.21 |
| Storage-net IP | 10.10.10.2/30 |

### 3.1 Disk inventory and pool design

The host came with a heterogeneous pile of scavenged drives (SATA SSD,
NVMe, and enterprise HDD, in various sizes). Rather than a single pool,
disks were grouped by role -- capacity vs. IOPS vs. write-durability -- and
split into three pools:

| Pool | Vdevs | Usable | Purpose |
|---|---|---|---|
| `tank-fast` | 2x mirror (SATA SSD + 1 NVMe) | ~903 GiB | VM disks (NFS), K3s persistent volumes (SMB) |
| `tank-bulk` | 2x mirror (1.82 TiB + 9.1 TiB HDD) + SLOG + L2ARC | ~10.8 TiB | Media library, backups |
| `scratch-nvme` | 1x single NVMe (no redundancy) | ~899 GiB | Scratch / working data, no protection needed |

**Design rationale:**
- All-flash `tank-fast` for latency-sensitive VM and container storage;
  spinning-disk `tank-bulk` for capacity-bound, mostly-sequential media
  workloads.
- `tank-bulk`'s SLOG and L2ARC are intentionally placed on the cheapest,
  most easily replaceable 250GB SATA SSDs in the disk inventory. SLOG
  absorbs the heaviest sustained write load (every sync write) and L2ARC
  absorbs continuous read-cache churn -- both are the most write-intensive
  roles in the system, so they were deliberately assigned to disposable
  hardware rather than to the more valuable NVMe/larger-capacity SSDs.
- `scratch-nvme` intentionally has no redundancy -- it holds only
  regenerable/transient data (container image cache, working files), so
  the extra usable capacity from skipping mirroring was preferred over
  protecting data that isn't worth protecting.
- Auto-TRIM is enabled on all three pools to prevent SSD write-amplification
  and performance degradation over time.

### 3.2 Datasets

| Dataset | recordsize | compression | atime | Notes |
|---|---|---|---|---|
| `tank-fast/vm-storage` | 32K | lz4 | off | Proxmox VM disks, NFS export |
| `tank-fast/k3s-pv` | 16K | lz4 | off | K3s PersistentVolumes, SMB export |
| `tank-bulk/media` | 1M | lz4 | off | Jellyfin media library, SMB export |
| `tank-bulk/downloads` | 1M | lz4 | off | qBittorrent working directory, SMB export -- deliberately separate from `media` (see §4.2) |
| `tank-bulk/backups` | 128K | lz4 | off | General-purpose backup target |
| `scratch-nvme/workdir` | 128K (default) | lz4 | off | Scratch space |

## 4. Storage protocol integration

### 4.1 Proxmox <-> TrueNAS (NFS)

- Export: `tank-fast/vm-storage` -> NFS share, `root`/`wheel` maproot,
  authorized network restricted to the Proxmox host's IP.
- Proxmox storage backend `tank-fast-nfs`, content types `images,rootdir`.
- Originally connected over the LAN (192.168.50.0/24); later re-pointed at
  the dedicated 10.10.10.0/30 link (see SS5) with zero VM downtime, using a
  lazy unmount (`umount -l`) followed by a storage disable/enable cycle.

### 4.2 K3s <-> TrueNAS (SMB, via `smb.csi.k8s.io`)

Two SMB shares are exposed from TrueNAS and consumed as K3s
`StorageClass`es via the existing `smb-csi` driver already in use for the
original `nas-smb` (ASUS router SMB share) StorageClass:

| StorageClass | Source | Reclaim policy | Consumers |
|---|---|---|---|
| `smb-tank-fast` | `//192.168.50.21/k3s-pv` | Delete | (validation PVC only) |
| `smb-tank-bulk-media` | `//192.168.50.21/media` | Retain | `jellyfin-media-v2` |
| `smb-tank-bulk-downloads` | `//192.168.50.21/downloads` | Retain | `qbittorrent-downloads-tank-bulk`, `filebrowser-downloads` |

`downloads` is a separate dataset/share from `media`, not a subfolder of it,
so that Jellyfin's library scanner never sees in-progress or partial
torrent downloads. Completed downloads are moved into `media` manually
(or by future automation). Two independent PVCs, in two different
namespaces (`qbittorrent` and `jellyfin`), point at the same underlying
SMB share -- PersistentVolumeClaims are namespace-scoped, so a PVC can't
be referenced across namespaces even when the backing storage is a shared
network resource; each consumer needs its own PVC against the same
StorageClass/source.

SMB (rather than NFS) was chosen for K3s storage for operational
consistency with the pre-existing `nas-smb` StorageClass, rather than
running two different storage protocols into the cluster.

A dedicated SMB service account (`k3s-smb`) with no shell/SSH/TrueNAS
access -- SMB access only -- owns both TrueNAS-side datasets, avoiding the
use of `root` for a network-facing service credential.

## 5. Dedicated storage network

Both hosts have a spare 2.5GbE NIC, previously unused, *in addition to* the
2.5GbE NIC each already uses to connect to the shared TP-Link switch (§1).
These spare NICs were direct-attached to each other with a single cable
(no switch in the path) and configured as a point-to-point `/30` network
(`10.10.10.0/30`) carrying only NFS traffic between Proxmox and TrueNAS:

- Proxmox: `nic0` -- `10.10.10.1/30` (persisted in `/etc/network/interfaces`)
- TrueNAS: `eno1` -- `10.10.10.2/30` (persisted via TrueNAS WebUI, per
  TrueNAS's config-management policy of WebUI/CLI/API only)

Rationale: isolates the highest-throughput, most latency-sensitive traffic
(VM disk I/O) from the shared LAN, freeing bandwidth for everything else
(K3s traffic, media streaming, management, Zigbee/MQTT, etc.) and reducing
the blast radius of LAN congestion or broadcast storms on VM storage
performance.

## 6. Media library migration

The pre-existing Jellyfin media library lived on an ASUS RT-AX86U router's
attached USB drive, exposed as **one SMB share per top-level folder**
(`Movies`, `Seriale`, `Bajki`, `qBittorrent`, `torrents`, etc.) rather than
a single share with subfolders -- a quirk of the router's AiCloud/Download
Master SMB implementation that initially caused `mount.cifs` failures
until identified via `smbclient -L`.

Migration approach:
1. Mount each relevant source share read-only directly on TrueNAS
   (`mount -t cifs ... -o ro`) to avoid a double network hop.
2. `rsync -avh --stats` each folder directly into `tank-bulk/media/<Folder>`,
   run in parallel in the background via `nohup`.
3. Verify byte-for-byte completeness via `du -sh` comparison and rsync's
   `--stats` output (files transferred, 0 deleted, matching total size).
4. Unmount and clean up temporary mount points.

| Folder | Size | Files |
|---|---|---|
| Movies | 83 GiB | 36 |
| Seriale | 387 GiB | 265 |
| Bajki | 92 GiB | 42 |
| **Total** | **562 GiB** | **343** |

Deliberately **excluded** from migration: `torrents` / `qBittorrent`
folders -- these are the download client's working directory, with a
different lifecycle than the curated media library, and are not part of
the Jellyfin-facing dataset. (qBittorrent's own working directory was
later given its own dedicated dataset, `tank-bulk/downloads` -- see §3.2
and §4.2 -- rather than being pointed at any of these old ASUS folders.)

After migration, `jellyfin` and `filebrowser` Deployments (previously
pointing at the now-decommissioned `nas-smb`/ASUS PVC) were re-pointed at
a new `jellyfin-media-v2` PVC backed by `smb-tank-bulk-media`, via
`kubectl patch` on each Deployment's volume `claimName` -- no manifest
rewrite needed, no image change, zero data loss.

## 7. Data protection -- periodic snapshots

| Dataset | Schedule | Retention | Rationale |
|---|---|---|---|
| `tank-fast/vm-storage` | 10:00, 15:00, 20:00 daily | 2 weeks | VM state changes frequently during active work hours |
| `tank-fast/k3s-pv` | 10:00, 15:00, 20:00 daily | 2 weeks | Same rationale -- active application data |
| `tank-bulk/media` | 10:00 daily | 1 week | Changes rarely (additions only); cheap insurance |
| `tank-bulk/backups` | 10:00 daily | 4 weeks | Backup target itself warrants longer retention |
| `scratch-nvme/workdir` | none | -- | Transient/regenerable data, not worth protecting |

Snapshot windows were deliberately restricted to daytime hours (10:00-20:00)
rather than round-the-clock, since infrastructure changes and active K3s
work happen during the day -- snapshotting overnight idle state adds
storage churn without added protection value.

## 8. Home Assistant / Zigbee subsystem

### 8.1 VM

| Item | Value |
|---|---|
| Host | Proxmox VE (VMID 100) |
| OS | Home Assistant OS 18.1 |
| CPU / RAM | 2 vCPU / 4GB |
| Disk | `tank-fast-nfs`, imported from official `haos_ova` qcow2 |
| Firmware | OVMF (UEFI), no Secure Boot key enrollment |
| USB passthrough | Zigbee USB coordinator (CH340 UART bridge, vendor:product `1a86:7523`), passed through by USB ID rather than physical port for restart-resilience |

### 8.2 Zigbee coordinator

The dongle is a generic "Zigbee 3.0 USB Dongle Plus" (marketed as
Zigbee+Thread+BLE capable). Despite the multiprotocol marketing copy, the
radio firmware identifies as a **zStack** (Texas Instruments CC253x-family)
adapter, not EmberZNet -- determined empirically after `ember` adapter
selection failed with repeated `ASH` handshake resets
(`HOST_FATAL_ERROR`), while `zstack` initialized cleanly on the first
attempt.

### 8.3 Stack

- **Mosquitto** (MQTT broker), Home Assistant add-on, `core-mosquitto`
- **Zigbee2MQTT**, Home Assistant add-on (community repo), port
  `/dev/ttyUSB0`, adapter `zstack`
- **MQTT integration** in Home Assistant Core, auto-discovered the local
  broker; Zigbee2MQTT's MQTT-discovery payloads surfaced all paired
  devices as HA entities automatically, no manual entity configuration
  needed.

### 8.4 Device inventory (19 devices / 200 entities)

| Category | Model | Count | Role in mesh |
|---|---|---|---|
| Smart plugs (metering) | Nous A7Z | 8 | Router (mains-powered) |
| Smart plugs (metering) | Girier JR-ZPM01 | 4 | Router (mains-powered) |
| Temp/humidity sensors | HOBEIAN ZG-227Z | 4 | End device (battery) |
| Weather station | Aqara WSDCGQ11LM | 1 | End device (battery) |
| Air quality sensor | Tuya TS0601 | 1 | End device (battery) |
| Bridge | Zigbee2MQTT coordinator | 1 | Coordinator |

Mains-powered plugs double as Zigbee mesh routers, extending range for the
battery-powered sensors -- placement of plugs (one per major appliance/
network device being power-monitored) incidentally produces reasonably
even mesh coverage across the flat. Link quality (LQI) ranged 47-178
across the fleet; the weakest link (47, kitchen fridge) is a candidate for
an additional router nearby if it proves unreliable in practice.


## 9. See also

Real-world incidents hit while building and operating this stack, and how
they were diagnosed and fixed, are tracked separately in
[docs/troubleshooting.md](troubleshooting.md) rather than here -- this
document describes the system's current, stable shape; that one is a
running log of what went wrong along the way.
