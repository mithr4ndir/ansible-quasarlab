#!/usr/bin/env bash
#
# Generate Prometheus file_sd targets from Ansible inventory and apply
# as a ConfigMap to the K8s cluster. Works with both static and dynamic
# inventory sources.
#
# Usage: ./scripts/sync-prometheus-targets.sh
#
set -euo pipefail

# Source 1Password service account token for dynamic inventory
export OP_SERVICE_ACCOUNT_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-$(cat ~/.config/op/service-account-token 2>/dev/null || true)}"
if [[ -n "$OP_SERVICE_ACCOUNT_TOKEN" ]] && command -v op &>/dev/null; then
    export PROXMOX_TOKEN_SECRET="${PROXMOX_TOKEN_SECRET:-$(op read "op://Infrastructure/Proxmox VE API/Ansible Inventory/token_secret" 2>/dev/null || true)}"
fi

NAMESPACE="monitoring"
CONFIGMAP_NAME="prometheus-file-sd-targets"
PORT="9100"

cd "$(dirname "$0")/.."

# Use ansible-inventory to dump all hosts with resolved IPs
ansible-inventory --list 2>/dev/null | python3 -c "
import json, sys

data = json.load(sys.stdin)
hostvars = data.get('_meta', {}).get('hostvars', {})

def get_hosts(group_name):
    group = data.get(group_name, {})
    hosts = set(group.get('hosts', []))
    for child in group.get('children', []):
        hosts.update(get_hosts(child))
    return hosts

linux_hosts = get_hosts('linux')

targets = []
for host in sorted(linux_hosts):
    hv = hostvars.get(host, {})
    ip = hv.get('ansible_host', '')
    if ip:
        targets.append({
            'targets': [f'{ip}:${PORT}'],
            'labels': {'instance': host}
        })

print(json.dumps(targets, indent=2))
" > /tmp/vm-targets.json

count=$(python3 -c "import json; print(len(json.load(open('/tmp/vm-targets.json'))))")
echo "Generated ${count} targets from inventory"

# Apply as ConfigMap
kubectl create configmap "${CONFIGMAP_NAME}" \
  --namespace="${NAMESPACE}" \
  --from-file=vm-targets.json=/tmp/vm-targets.json \
  --dry-run=client -o yaml | kubectl apply -f -

echo "ConfigMap ${CONFIGMAP_NAME} updated in ${NAMESPACE}"
rm /tmp/vm-targets.json
