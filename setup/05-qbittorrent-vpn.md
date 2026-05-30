# Part 5: qBittorrent with ProtonVPN WireGuard via Gluetun

## Goal

Deploy qBittorrent on k3s cluster with all traffic routed through
ProtonVPN using WireGuard protocol. Gluetun acts as a VPN sidecar
container sharing the network namespace with qBittorrent.

## Architecture
Internet
│
ProtonVPN WireGuard (Norway/Netherlands)
│
Gluetun sidecar container
│ (shared network namespace)
qBittorrent container
│
MetalLB LoadBalancer → 192.168.50.54:8080

## Prerequisites

- K3s cluster running
- MetalLB configured with IP pool 192.168.50.50-60
- ProtonVPN account with WireGuard config generated
- WireGuard config from account.proton.me → WireGuard

## WireGuard Config Generation

1. Go to account.proton.me → WireGuard
2. Device name: `pi-homelab`
3. Platform: `GNU/Linux`
4. Enable NAT-PMP (port forwarding)
5. Select server (Norway recommended)
6. Download `.conf` file

Extract values for `.env`:
WIREGUARD_PRIVATE_KEY=<PrivateKey from conf>
WIREGUARD_PUBLIC_KEY=<PublicKey from conf>
WIREGUARD_ENDPOINT=<Endpoint IP from conf>
WIREGUARD_ADDRESS=<Address from conf>

## Kubernetes Manifests

### namespace.yaml (implicit - created with kubectl)

### pvc.yaml

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: qbittorrent-config
  namespace: qbittorrent
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: qbittorrent-downloads
  namespace: qbittorrent
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: 50Gi
```

### deployment.yaml

Key configuration:
- Gluetun as sidecar with NET_ADMIN capability
- VPN_SERVICE_PROVIDER=custom with WireGuard
- FIREWALL_INPUT_PORTS=8080 to allow WebUI access
- HEALTH_VPN_DURATION_INITIAL=120s to give qBittorrent time to start

### service.yaml

```yaml
apiVersion: v1
kind: Service
metadata:
  name: qbittorrent
  namespace: qbittorrent
spec:
  selector:
    app: qbittorrent
  ports:
  - port: 8080
    targetPort: 8080
    name: webui
  type: LoadBalancer
  loadBalancerIP: 192.168.50.54
```

**Critical:** Use LoadBalancer with MetalLB, NOT NodePort or port-forward.
qBittorrent 5.x validates Host header port — NodePort (30080) causes
port mismatch with internal port (8080) → Unauthorized.

## Deployment

```bash
source .env

kubectl create namespace qbittorrent

kubectl create secret generic protonvpn-wireguard \
  --from-literal=private-key="$WIREGUARD_PRIVATE_KEY" \
  --namespace qbittorrent

kubectl apply -f k8s/qbittorrent/
```

## Initial Login

qBittorrent 5.x generates a temporary password on first run.
Get it from container stdout logs (NOT from qbittorrent.log file):

```bash
kubectl logs -n qbittorrent \
  $(kubectl get pods -n qbittorrent -o name | head -1) \
  -c qbittorrent | grep -i password
```

Access WebUI at: `http://192.168.50.54:8080`

After login, immediately set permanent password in:
Tools → Options → WebUI → Password

## Verify VPN is Working

In qBittorrent WebUI:
Tools → Options → Advanced → Network Interface → select `tun0`

Check logs for WireGuard IP:
```bash
kubectl exec -n qbittorrent \
  $(kubectl get pods -n qbittorrent -o name | head -1) \
  -c qbittorrent -- \
  grep "10.2.0" /config/qBittorrent/logs/qbittorrent.log
```

Should show `Successfully listening on IP: "10.2.0.2"` — the WireGuard interface.

---

## Problems Encountered & Solutions

### 1. OpenVPN AUTH_FAILED

**Symptom:** Gluetun logs show `AUTH: Received control message: AUTH_FAILED`

**Root cause:** OpenVPN credentials (username/password) are separate from
WireGuard credentials. ProtonVPN uses different auth for each protocol.

**Solution:** Switch from OpenVPN to WireGuard. Generate WireGuard config
from account.proton.me and use `VPN_TYPE=wireguard` with
`VPN_SERVICE_PROVIDER=custom`.

### 2. WireGuard healthcheck timeout

**Symptom:** `restarting VPN because it failed to pass the healthcheck:
dial tcp4: lookup github.com: i/o timeout`

**Root cause:** Gluetun's built-in DNS server couldn't resolve hostnames
through the WireGuard tunnel due to DNS configuration conflicts.

**Solution:** Remove all DNS-related env vars (DNS_SERVER, DOT,
DNS_UPSTREAM_PLAIN_ADDRESSES) and let Gluetun use defaults.
Add `HEALTH_VPN_DURATION_INITIAL=120s` to give tunnel time to stabilize.

### 3. DNS settings conflict

**Symptom:** `ERROR dns settings: upstream type dot must be plain
if the built-in DNS server is disabled`

**Root cause:** Conflicting environment variables set via `kubectl set env`
across multiple attempts. `DOT=off` conflicted with default DoT upstream.

**Solution:** Always update deployment.yaml directly instead of using
`kubectl set env` — avoids accumulating conflicting variables.
Verify with: `kubectl get deployment -o yaml | grep -A2 "env:"`

### 4. qBittorrent crashes in loop (lockfile)

**Symptom:** qBittorrent starts and immediately exits, repeating every second.
Log shows: `qBittorrent termination initiated` right after start.

**Root cause:** Stale lockfile in `/config/qBittorrent/lockfile` from
previous crashed session. qBittorrent detects another instance running
and exits immediately.

**Solution:**
```bash
kubectl exec -n qbittorrent \
  $(kubectl get pods -n qbittorrent -o name | head -1) \
  -c qbittorrent -- rm -f /config/qBittorrent/lockfile
```

**Prevention:** Add init container or startup script to remove lockfile.

### 5. Unauthorized - no login form

**Symptom:** Browser shows only "Unauthorized" text without login form.

**Root cause (A):** qBittorrent 5.x validates Host header. When using
NodePort (30080), browser sends `Host: 192.168.50.12:30080` but
qBittorrent listens on port 8080 → port mismatch → rejected.

**Solution:** Use MetalLB LoadBalancer on port 8080 instead of NodePort.
Browser sends `Host: 192.168.50.54:8080` which matches.

**Root cause (B):** WebUI\Address=* in config causes qBittorrent script
to set listen address to `localhost` only — blocking external access.

**Solution:**
```bash
kubectl exec ... -- sed -i \
  's/WebUI\\Address=\*/WebUI\\Address=0.0.0.0/' \
  /config/qBittorrent/qBittorrent.conf
```

### 6. Gluetun blocks WebUI port

**Symptom:** Port-forward connects but returns empty response or
connection refused inside namespace.

**Root cause:** Gluetun firewall blocks all inbound traffic not through VPN.

**Solution:** Add `FIREWALL_INPUT_PORTS=8080` env var to Gluetun container.

### 7. Temporary password not visible

**Symptom:** `kubectl logs ... | grep password` returns nothing.

**Root cause:** qBittorrent 5.x writes temporary password to stdout
(visible in `kubectl logs`) NOT to `/config/qBittorrent/logs/qbittorrent.log`.
The file log and stdout are separate.

**Solution:** Always check stdout via `kubectl logs`, not the file log.

### 8. VPN service provider mismatch

**Symptom:** `ERROR: requested tags [tag:k8s] are invalid` or
server selection errors with `protonvpn` provider.

**Root cause:** ProtonVPN provider in Gluetun uses its own server list
and tries random servers — but WireGuard keys are server-specific.

**Solution:** Use `VPN_SERVICE_PROVIDER=custom` with explicit endpoint:
WIREGUARD_ENDPOINT_IP=169.150.218.75
WIREGUARD_ENDPOINT_PORT=51820
WIREGUARD_PUBLIC_KEY=<server public key from .conf>

---

## Lessons Learned

1. **Use LoadBalancer over NodePort** for services with host header validation
2. **Check stdout logs** (`kubectl logs`) separately from application log files
3. **Update deployment.yaml directly** instead of `kubectl set env` to avoid
   variable conflicts
4. **Lockfile cleanup** is critical for stateful apps with PVC storage
5. **WireGuard keys are server-specific** — always use `custom` provider
   with explicit endpoint when you have a specific config file
6. **Gluetun and qBittorrent share network namespace** — Gluetun firewall
   affects ALL inbound/outbound traffic including WebUI
