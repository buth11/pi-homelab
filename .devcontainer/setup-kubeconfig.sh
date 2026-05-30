#!/bin/bash
echo "Setting up kubeconfig..."
mkdir -p ~/.kube
ssh buth11@192.168.50.10 "sudo cat /etc/rancher/k3s/k3s.yaml" > ~/.kube/config
sed -i 's/127.0.0.1/192.168.50.10/g' ~/.kube/config
chmod 600 ~/.kube/config
echo "Done! Testing connection..."
kubectl get nodes
