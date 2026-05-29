# Part 1: Node Preparation & K3s Installation

## Hardware

| Hostname | Model | Role | RAM | Storage |
|----------|-------|------|-----|---------|
| pi4-master | Raspberry Pi 4B | K3s control-plane | 4GB | 128GB SanDisk Extreme A2 |
| pi3-worker1 | Raspberry Pi 3B+ | K3s worker | 1GB | 32GB SanDisk Ultra |
| pi4-worker2 | Raspberry Pi 4B | K3s worker | 4GB | 64GB Kingston Canvas Select Plus |

## OS Installation

**OS:** Raspberry Pi OS Lite 64-bit (Bookworm / Debian 12)

> **Note:** In Raspberry Pi Imager v2.0.7, "Legacy 64-bit" = Bookworm (Debian 12).
> "Current" = Trixie (Debian 13) — avoided as too new for production-like homelab.

Configured via Raspberry Pi Imager:
- SSH enabled with password authentication
- Locale: Europe/Warsaw
- WiFi: not configured (cluster uses LAN only)

## Network

All nodes connected via ethernet to home router.
Router subnet: `192.168.1.0/24`

| Hostname | IP |
|----------|----|
| pi4-master | 192.168.1.105 |
| pi3-worker1 | 192.168.1.102 |
| pi4-worker2 | 192.168.1.107 |

Static IPs configured via DHCP reservation on router (by MAC address).

## Node Preparation

Performed on all nodes before k3s installation.

### 1. System update

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl wget git
```

### 2. Disable swap

Raspberry Pi OS uses `dphys-swapfile` service — editing `/etc/fstab` alone is not enough.

```bash
sudo systemctl disable dphys-swapfile
sudo systemctl stop dphys-swapfile
sudo dphys-swapfile swapoff
```

Verify:
```bash
free -h
# Swap line should show: 0B  0B  0B
```

### 3. Enable cgroups

K3s requires memory cgroups enabled. On Raspberry Pi OS Bookworm this requires
editing the kernel command line.

```bash
sudo nano /boot/firmware/cmdline.txt
```

Append to the end of the existing line (do NOT add a new line):
cgroup_disable= cgroup_enable=cpuset cgroup_enable=memory cgroup_memory=1

> **Problem encountered:** Raspberry Pi firmware DTB files contain
> `cgroup_disable=memory` which overrides cmdline.txt settings.
> Adding `cgroup_disable=` (empty value) before our parameters neutralizes it.
> This affects Pi 3B+, Pi 4B and Zero 2W on Bookworm.

Verify after reboot:
```bash
cat /proc/cgroups | grep memory
# Should show: memory  0  X  1
```

> **Note:** Despite the fix, `cat /proc/cgroups | grep memory` returned no output
> on our Pi 3/4 nodes. K3s v1.35.5 handles this gracefully and starts anyway.

### 4. Fix /etc/hosts after hostname change

After changing hostname with `hostnamectl`, add entry to `/etc/hosts`:

```bash
echo "127.0.0.1 pi4-master" | sudo tee -a /etc/hosts
```

## K3s Installation

### Architecture decision: Pi 4 as master, not Pi 3

> **Problem encountered:** Initially installed k3s server on Pi 3B+ (1GB RAM).
> After installation, only **35MB RAM** was free — k3s server alone consumed ~841MB.
> This caused:
> - "Slow SQL" warnings in etcd
> - "housekeeping took too long" errors in kubelet  
> - Worker nodes unable to retrieve CA certificates from master
> - Cluster effectively unusable
>
> **Solution:** Moved control-plane to Pi 4 (4GB RAM).
> After migration, Pi 4 master had **2.4GB free RAM** with k3s running.
> Pi 3B+ was reassigned as worker node where resource requirements are much lower.
>
> **Lesson learned:** K3s server (control-plane) needs minimum 1GB RAM comfortable,
> 2GB+ recommended. Pi 3 with 1GB is only suitable as a worker node.

### Install k3s server (on pi4-master)

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--flannel-iface eth0" sh -
```

`--flannel-iface eth0` forces pod network traffic through ethernet interface,
not WiFi — critical for cluster stability.

Verify:
```bash
sudo systemctl status k3s
sudo kubectl get nodes
```

### Join worker nodes

Get token from master:
```bash
sudo cat /var/lib/rancher/k3s/server/node-token
```

On each worker node:
```bash
curl -sfL https://get.k3s.io | \
  K3S_URL=https://192.168.1.105:6443 \
  K3S_TOKEN=<TOKEN> \
  INSTALL_K3S_EXEC="--flannel-iface eth0" \
  sh -
```

> **Problem encountered:** First attempt installed k3s without K3S_URL parameter
> on worker nodes. Each node started its own independent cluster instead of
> joining the master. Workers showed "connection refused" to 127.0.0.1:6443
> instead of connecting to master IP.
>
> **Solution:** Uninstall k3s on all nodes and reinstall workers with correct
> K3S_URL and K3S_TOKEN parameters.

### Verify cluster

```bash
sudo kubectl get nodes
# Expected output:
# NAME          STATUS   ROLES           AGE   VERSION
# pi4-master    Ready    control-plane   Xm    v1.35.5+k3s1
# pi3-worker1   Ready    <none>          Xm    v1.35.5+k3s1
# pi4-worker2   Ready    <none>          Xm    v1.35.5+k3s1

sudo kubectl get pods -A
# All pods should show Running or Completed
```

## Additional issues encountered

### Router LAN4/WAN port

One router port was configured as WAN (dual-function LAN4/WAN port on TP-Link
4G router). Device connected to this port was unreachable from LAN.
Workaround: used remaining LAN ports only.

### SD card PARTUUID conflict

When flashing multiple SD cards with Raspberry Pi Imager in quick succession,
cards may receive identical PARTUUIDs. This causes boot failure as the kernel
cannot find the correct root partition.

**Symptom:** Node fails to boot after cmdline.txt edit, SSH unreachable.

**Fix:** Mount the SD card on another machine and correct the PARTUUID:
```bash
# Find real PARTUUID
sudo blkid /dev/mmcblk0p2

# Mount boot partition and fix cmdline.txt
sudo mkdir -p /mnt/piboot
sudo mount /dev/mmcblk0p1 /mnt/piboot
sudo nano /mnt/piboot/cmdline.txt
# Update root=PARTUUID=XXXX to match blkid output
sudo umount /mnt/piboot
```
