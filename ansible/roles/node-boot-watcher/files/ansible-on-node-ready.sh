#!/bin/bash
# Watches for kubelet "Starting" events (kubelet (re)started on some node --
# possibly, but not necessarily, because the node itself rebooted onto a
# different kernel) and re-runs site.yml scoped to that one node.
#
# Note: the k8s "NodeReady" event reason only fires on a genuine
# NotReady->Ready condition transition, which a fast kubelet restart does not
# always trigger (confirmed empirically: a plain `systemctl restart k3s-agent`
# never flips the Ready condition, so no NodeReady event is ever emitted).
# "Starting" / "Starting kubelet." fires on every kubelet start, cold boot or
# not, which is what we actually want here -- cheap and idempotent to re-run
# Ansible a bit too often, expensive to miss the one time it mattered.
#
# Long-running, restarted by systemd on exit/crash.
set -uo pipefail

REPO_ANSIBLE_DIR="/mnt/truenas-syncthing/Pi-Homelab/ansible"
KUBECONFIG="/etc/rancher/k3s/k3s.yaml"
KUBECTL="/usr/local/bin/kubectl"
STATE_DIR="/run/ansible-node-watcher"
DEBOUNCE_SECONDS=120

mkdir -p "$STATE_DIR"

echo "ansible-on-node-ready: watcher starting"

sudo "$KUBECTL" --kubeconfig="$KUBECONFIG" get events -A \
    --field-selector reason=Starting \
    --watch-only \
    -o custom-columns=NODE:.involvedObject.name --no-headers |
while read -r node; do
    [ -z "$node" ] && continue

    last_run_file="$STATE_DIR/$node.last_run"
    now=$(date +%s)
    last_run=$(cat "$last_run_file" 2>/dev/null || echo 0)
    if [ $((now - last_run)) -lt "$DEBOUNCE_SECONDS" ]; then
        echo "ansible-on-node-ready: Starting event for '$node', but ran ${DEBOUNCE_SECONDS}s ago or less -- skipping (debounce)"
        continue
    fi
    echo "$now" > "$last_run_file"

    echo "ansible-on-node-ready: Starting event for '$node', running ansible-playbook --limit $node"
    (cd "$REPO_ANSIBLE_DIR" && ansible-playbook site.yml --limit "$node")
    echo "ansible-on-node-ready: run for '$node' finished with exit $?"
done

echo "ansible-on-node-ready: kubectl watch stream ended, exiting (systemd will restart)"
