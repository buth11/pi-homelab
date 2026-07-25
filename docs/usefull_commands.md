# Useful Commands

## Pi-hole

```bash
# Change Pi-hole web password
kubectl exec -n pihole deployment/pihole -- pihole setpassword 'password'

# Check Pi-hole DNS listening mode
kubectl exec -n pihole deployment/pihole -- bash -c "pihole-FTL --config dns.listeningMode"

# Access Pi-hole web panel
kubectl port-forward -n pihole svc/pihole-web 8080:80
# Then open: http://localhost:8080/admin

# Test DNS resolution through Pi-hole
nslookup google.com 192.168.50.53
```

## kubectl

```bash
# Get all nodes
kubectl get nodes

# Get all pods in all namespaces
kubectl get pods -A

# Get pods in specific namespace
kubectl get pods -n pihole

# Get services
kubectl get svc -n pihole

# Check pod logs
kubectl logs -n pihole deployment/pihole

# Restart deployment
kubectl rollout restart deployment/pihole -n pihole

# Execute command in pod
kubectl exec -n pihole deployment/pihole -- <command>

# Follow logs live while reproducing an issue
kubectl logs <pod> -n <namespace> -f

# Patch a single field on a running Deployment without rewriting the whole manifest
# (index refers to position in the volumes/containers list -- check first with
# `kubectl get deployment <name> -n <ns> -o jsonpath='{.spec.template.spec.volumes[*].name}'`)
kubectl patch deployment <name> -n <namespace> --type='json' -p='[
  {"op": "replace", "path": "/spec/template/spec/volumes/0/persistentVolumeClaim/claimName", "value": "new-pvc-name"}
]'
```

## k3s (on nodes via SSH)

```bash
# Check k3s server status
sudo systemctl status k3s

# Check k3s agent status  
sudo systemctl status k3s-agent

# Get node token (on master)
sudo cat /var/lib/rancher/k3s/server/node-token

# View k3s logs
sudo journalctl -u k3s -n 50 --no-pager
```

## Cluster management

```bash
# Update kubeconfig (Windows PowerShell)
ssh buth11@192.168.50.10 "sudo cat /etc/rancher/k3s/k3s.yaml" > "$HOME\.kube\config"
(Get-Content "$HOME\.kube\config") -replace '127.0.0.1', '192.168.50.10' | Set-Content "$HOME\.kube\config"

# Update kubeconfig (devcontainer)
ssh buth11@192.168.50.10 "sudo cat /etc/rancher/k3s/k3s.yaml" > ~/.kube/config
sed -i 's/127.0.0.1/192.168.50.10/g' ~/.kube/config
chmod 600 ~/.kube/config

# Cluster shutdown (pi4-master, pi4-worker2, g3-worker3 -- no Pi3, decommissioned)
ssh buth11@192.168.50.10 "sudo shutdown -h now" &
ssh buth11@192.168.50.12 "sudo shutdown -h now" &
ssh buth11@192.168.50.13 "sudo shutdown -h now"
```

## Network

```bash
# Check Pi-hole DNS service
kubectl get svc -n pihole pihole-dns

# Check MetalLB IP pool
kubectl get ipaddresspool -n metallb-system

# Check MetalLB speakers
kubectl get pods -n metallb-system -o wide
```

## Grafana

```bash
# Reset Grafana admin password
kubectl exec -n monitoring deployment/kube-prometheus-stack-grafana -c grafana -- /usr/share/grafana/bin/grafana cli admin reset-admin-password 'NewPassword'

# Get Grafana admin password from secret
kubectl get secret -n monitoring kube-prometheus-stack-grafana -o jsonpath="{.data.admin-password}" | base64 -d

# Access Grafana web panel
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
# Then open: http://localhost:3000
# Login: admin
```

## Power Management (deployments)
```bash
# Stop all non-essential services (save power)
kubectl scale deployment qbittorrent -n qbittorrent --replicas=0
kubectl scale deployment jellyfin -n jellyfin --replicas=0

# Start all services
kubectl scale deployment qbittorrent -n qbittorrent --replicas=1
kubectl scale deployment jellyfin -n jellyfin --replicas=1

# Check all deployments status
kubectl get deployments -A
```

## qBittorrent
```bash
# Get temporary WebUI password
kubectl logs -n qbittorrent $(kubectl get pods -n qbittorrent -o name | head -1) -c qbittorrent | grep -i password

# Remove lockfile (if qBittorrent crashes in loop)
kubectl exec -n qbittorrent $(kubectl get pods -n qbittorrent -o name | head -1) -c qbittorrent -- rm -f /config/qBittorrent/lockfile

# Check VPN IP
kubectl exec -n qbittorrent $(kubectl get pods -n qbittorrent -o name | head -1) -c qbittorrent -- curl -s https://ipinfo.io/ip
```

## WireGuard VPN (on G3 Mini)
```bash
# Connect VPN
sudo wg-quick up proton

# Disconnect VPN
sudo wg-quick down proton

# Check VPN status
sudo wg show proton

# Check current IP
curl -s https://ipinfo.io/ip
```

## Node Management
```bash
# Drain and remove node from cluster
kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data --force
kubectl delete node <node-name>

# Mark node as unschedulable (maintenance)
kubectl cordon <node-name>

# Mark node as schedulable again
kubectl uncordon <node-name>
```

## PVC / storage troubleshooting

```bash
# Check PVC status and which pod is using it
kubectl get pvc -n <namespace>
kubectl describe pvc <pvc-name> -n <namespace>

# PVC stuck in Pending with only "WaitForFirstConsumer" and no Provisioning
# event ever appearing in the provisioner's logs? Check whether the consuming
# Deployment uses nodeName instead of nodeSelector -- nodeName bypasses the
# scheduler, and it's the scheduler that annotates a WaitForFirstConsumer PVC
# with volume.kubernetes.io/selected-node. Without that annotation the
# provisioner has nothing to act on. Fix: use nodeSelector, not nodeName.
kubectl get deployment <name> -n <namespace> -o yaml | grep -A2 "nodeName\|nodeSelector"
grep -rn "nodeName" k8s/   # audit the whole repo for the same mistake

# local-path-provisioner logs (where PVCs on the local-path StorageClass
# actually get created)
kubectl logs -n kube-system -l app=local-path-provisioner --tail=50

# Restart the provisioner (rarely the actual fix, but cheap to rule out)
kubectl delete pod -n kube-system -l app=local-path-provisioner

# List every PVC on local-path across the cluster
kubectl get pvc -A | grep local-path

# Recreate a stuck PVC cleanly (scale to 0 first so nothing races to consume
# the PVC while it's being deleted/recreated)
kubectl scale deployment <name> -n <namespace> --replicas=0
kubectl delete pvc <pvc-name> -n <namespace>
kubectl apply -f <pvc-manifest>.yaml
kubectl scale deployment <name> -n <namespace> --replicas=1
```

## TrueNAS / ZFS (SSH into TrueNAS, prefix with sudo as truenas_admin)

```bash
# Pool and dataset health
sudo zpool status <pool>
sudo zpool status               # all pools
sudo zfs list                   # all datasets + usage

# Auto-TRIM status (should be "on" for all pools with SSD/NVMe members)
sudo zpool get autotrim <pool>

# Mount a remote SMB share temporarily (e.g. to migrate data off it)
sudo mkdir -p /mnt/tmp-source
sudo mount -t cifs //<host>/<share> /mnt/tmp-source -o username=<user>,password=<pass>,vers=2.0,ro
sudo umount /mnt/tmp-source
sudo rmdir /mnt/tmp-source

# List SMB shares actually exposed by a host (useful when a router/NAS
# exposes one share per folder instead of one share with subfolders)
smbclient -L //<host> -U <user>

# rsync a folder in, with live progress, running in the background so it
# survives a disconnect
nohup sudo rsync -avh --stats /mnt/tmp-source/ /mnt/tank-bulk/media/<Folder>/ > ~/rsync-<name>.log 2>&1 &
tail -f ~/rsync-<name>.log
ps aux | grep rsync             # confirm it's still alive after a reconnect
du -sh /mnt/tank-bulk/media/<Folder>   # check progress
```

## Proxmox VE

```bash
# VM status / lifecycle
qm status <vmid>
qm start <vmid>
qm stop <vmid>                  # full stop, releases file handles -- safer
                                 # than `qm reset` right after a storage change
qm reset <vmid>                 # hard reset, avoid right after re-pointing
                                 # storage the VM is actively using

# Storage
pvesm status                    # all configured storage backends
qm config <vmid>                # full VM config, incl. which storage each disk uses

# Changing the "server" field of an existing storage entry isn't allowed via
# `pvesm set` or the WebUI (fixed parameter) -- edit directly instead, this is
# a fully supported way to manage Proxmox config:
nano /etc/pve/storage.cfg

# If re-pointing NFS storage that's mounted under an active VM: stop the VM
# first if at all possible. If it must be done live, expect a lazy unmount
# to potentially leave the VM's filesystem remounted read-only afterwards --
# check `qm status` and the VM console, and be ready to `qm stop` / `qm start`
# (not `qm reset`) it once the new mount has settled.
umount -l /mnt/pve/<storage-id>
pvesm set <storage-id> --disable 1
pvesm set <storage-id> --disable 0

# USB passthrough for a device (Zigbee dongles etc.) -- pass by vendor:product
# ID, not physical port, so it survives being moved to a different USB port
lsusb                                          # find the device's ID
qm set <vmid> --usb0 host=<vendor>:<product>

# Bring up a spare/secondary NIC (e.g. for a dedicated storage network)
ip link set <iface> up
ip addr add <ip>/<prefix> dev <iface>
# Persist in /etc/network/interfaces, then:
ifreload -a
```

## Home Assistant / Zigbee2MQTT

```bash
# From the HAOS console (`ha >` prompt) -- drop to a full root shell
login

# Supervisor / add-on status and logs from that shell
ha supervisor info
ha addons list
ha addons info <slug>
ha addons logs <slug>

# If an add-on's Ingress panel returns 502 but you're not sure whether the
# add-on itself is actually stuck: check its logs directly via `ha addons
# logs` rather than trusting the web UI, which can cache a dead session.
```
