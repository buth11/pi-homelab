# Part 4: Moving Cluster to New Network

## Context

Cluster was physically moved from work location (192.168.1.x) to home (192.168.50.x).
New static IPs assigned via DHCP reservation on home router.

## New Network Layout

| Hostname     | Role          | IP             | MAC               |
|-------------|---------------|----------------|-------------------|
| pi4-master  | k3s master    | 192.168.50.10  | dc:a6:32:66:14:af |
| pi3-worker1 | k3s worker    | 192.168.50.11  | b8:27:eb:b7:d9:26 |
| pi4-worker2 | k3s worker    | 192.168.50.12  | dc:a6:32:09:06:ab |

## Problems Encountered

### WiFi and LAN both active

After moving to home network, all Pi had both eth0 and wlan0 active,
each with different IPs. This caused routing confusion.

Solution - disable WiFi permanently on all nodes:
```bash
sudo nmcli radio wifi off
```

### k3s state.db contained old IPs

k3s stores node IPs in an internal SQLite database. After network change,
k3s server crashed with:
"failed to find interface with specified node ip"

The database contained hundreds of references to old 192.168.1.x IPs
and cannot be edited directly.

Solution: full reinstall of k3s on all nodes.

On master:
```bash
sudo /usr/local/bin/k3s-uninstall.sh
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--flannel-iface eth0" sh -
sudo cat /var/lib/rancher/k3s/server/node-token
```

On each worker:
```bash
sudo /usr/local/bin/k3s-agent-uninstall.sh
curl -sfL https://get.k3s.io | \
  K3S_URL=https://192.168.50.10:6443 \
  K3S_TOKEN=<TOKEN> \
  INSTALL_K3S_EXEC="--flannel-iface eth0" \
  sh -
```

### Lesson learned

When moving a k3s cluster to a new network, always do a full reinstall.
Do not attempt to edit state.db or patch IP references manually.

## Update kubeconfig on workstation

### Linux/Fedora
```bash
scp buth11@192.168.50.10:~/k3s.yaml ~/.kube/config
sed -i 's/127.0.0.1/192.168.50.10/g' ~/.kube/config
```

### Windows
```powershell
ssh buth11@192.168.50.10 "sudo cat /etc/rancher/k3s/k3s.yaml" > "$HOME\.kube\config"
(Get-Content "$HOME\.kube\config") -replace '127.0.0.1', '192.168.50.10' | Set-Content "$HOME\.kube\config"
```

## Verify

```powershell
kubectl get nodes
# NAME          STATUS   ROLES           AGE   VERSION
# pi3-worker1   Ready    <none>          39s   v1.35.5+k3s1
# pi4-master    Ready    control-plane   6m    v1.35.5+k3s1
# pi4-worker2   Ready    <none>          59s   v1.35.5+k3s1
```
