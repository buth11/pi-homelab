# Part 2: Local kubectl Access from Workstation

## Goal

Manage the k3s cluster directly from the workstation (GMKtec/Fedora)
without SSH-ing into the master node every time.

## Steps

### 1. Copy kubeconfig from master

K3s stores kubeconfig at `/etc/rancher/k3s/k3s.yaml` owned by root.
Copy it to a user-accessible location first:

```bash
# On master
sudo cp /etc/rancher/k3s/k3s.yaml ~/k3s.yaml
sudo chown buth11:buth11 ~/k3s.yaml
```

### 2. Transfer to workstation

```bash
# On workstation
mkdir -p ~/.kube
scp buth11@192.168.1.105:~/k3s.yaml ~/.kube/config

# Replace localhost with master IP
sed -i 's/127.0.0.1/192.168.1.105/g' ~/.kube/config

# Secure the file - kubectl refuses world-readable kubeconfig
chmod 600 ~/.kube/config

# Cleanup temp file on master
ssh buth11@192.168.1.105 "rm ~/k3s.yaml"
```

### 3. Verify

```bash
kubectl get nodes
# Should show all 3 nodes as Ready without SSH
```

## Why this works

K3s kubeconfig contains the API server address, TLS certificates and
authentication token. By default it points to `127.0.0.1` (localhost)
assuming kubectl runs on the same machine as k3s server.

Replacing `127.0.0.1` with the master's LAN IP allows kubectl to reach
the API server from any machine on the same network.

## Devcontainer

Repository includes a devcontainer configuration at `.devcontainer/devcontainer.json`
that provides a pre-configured development environment with:

- `kubectl` — Kubernetes CLI
- `helm` — Kubernetes package manager  
- `ansible` — node automation
- `gh` — GitHub CLI
- VS Code extensions for Kubernetes, YAML, Docker

The devcontainer mounts `~/.kube` from the host so kubeconfig is
automatically available inside the container.

### Usage

1. Install VS Code with Dev Containers extension
2. Open repo in VS Code
3. Click "Reopen in Container" when prompted
4. All tools are immediately available
