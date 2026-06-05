#!/usr/bin/env bash
# Deploy the homelab dashboard to the k3s cluster.
# Run from repo root: ./dashboard/deploy.sh
set -euo pipefail

NAMESPACE=dashboard
K8S_DIR=k8s/dashboard

echo "==> Applying manifests..."
kubectl apply -f "$K8S_DIR/namespace.yaml"
kubectl apply -f "$K8S_DIR/rbac.yaml"
kubectl apply -f "$K8S_DIR/configmap.yaml"
kubectl apply -f "$K8S_DIR/service.yaml"

echo ""
echo "==> Checking SSH key secret..."
if ! kubectl get secret dashboard-ssh-key -n "$NAMESPACE" &>/dev/null; then
  echo "   SSH key secret not found. Creating from ~/.ssh/id_rsa..."
  if [[ -f ~/.ssh/id_rsa ]]; then
    kubectl create secret generic dashboard-ssh-key \
      -n "$NAMESPACE" \
      --from-file=id_rsa=~/.ssh/id_rsa
    echo "   Secret created."
  else
    echo "   WARNING: ~/.ssh/id_rsa not found."
    echo "   Create secret manually:"
    echo "     kubectl create secret generic dashboard-ssh-key -n dashboard --from-file=id_rsa=/path/to/key"
    echo "   Then re-run this script."
  fi
else
  echo "   SSH key secret already exists."
fi

echo ""
echo "==> Applying deployments..."
kubectl apply -f "$K8S_DIR/backend-deployment.yaml"
kubectl apply -f "$K8S_DIR/frontend-deployment.yaml"

echo ""
echo "==> Waiting for rollout..."
kubectl rollout status deployment/dashboard-backend -n "$NAMESPACE" --timeout=120s
kubectl rollout status deployment/dashboard-frontend -n "$NAMESPACE" --timeout=120s

echo ""
echo "==> Dashboard deployed!"
echo "   URL: http://192.168.50.58"
echo ""
echo "   Pods:"
kubectl get pods -n "$NAMESPACE"
echo ""
echo "   Services:"
kubectl get svc -n "$NAMESPACE"
echo ""
echo "IMPORTANT: Fill in G3 MAC address if not done yet:"
echo "  kubectl patch configmap dashboard-config -n dashboard --type=merge \\"
echo "    -p '{\"data\":{\"G3_MAC\":\"YOUR:MAC:HERE\"}}'"
echo "  kubectl rollout restart deployment/dashboard-backend -n dashboard"
