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
# Shutdown entire cluster
ssh buth11@192.168.50.10 "sudo shutdown -h now" &
ssh buth11@192.168.50.11 "sudo shutdown -h now" &
ssh buth11@192.168.50.12 "sudo shutdown -h now"

# Update kubeconfig (Windows PowerShell)
ssh buth11@192.168.50.10 "sudo cat /etc/rancher/k3s/k3s.yaml" > "$HOME\.kube\config"
(Get-Content "$HOME\.kube\config") -replace '127.0.0.1', '192.168.50.10' | Set-Content "$HOME\.kube\config"

# Update kubeconfig (devcontainer)
ssh buth11@192.168.50.10 "sudo cat /etc/rancher/k3s/k3s.yaml" > ~/.kube/config
sed -i 's/127.0.0.1/192.168.50.10/g' ~/.kube/config
chmod 600 ~/.kube/config
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
