#!/usr/bin/env bash
#
# Generate Prometheus file_sd targets from Ansible inventory and apply
# as a ConfigMap to the K8s cluster. Run after inventory changes.
#
# Usage: ./scripts/sync-prometheus-targets.sh [inventory_file]
#
set -euo pipefail

INVENTORY="${1:-inventory.ini}"
NAMESPACE="monitoring"
CONFIGMAP_NAME="prometheus-file-sd-targets"
PORT="9100"

cd "$(dirname "$0")/.."

# Parse inventory.ini and generate targets JSON
python3 -c "
import re, json, sys

targets = []
current_group = None
skip_groups = {'linux:children', 'proxmox:vars'}

with open('${INVENTORY}') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # Group header
        m = re.match(r'\[(.+)\]', line)
        if m:
            current_group = m.group(1)
            continue

        # Skip meta-groups and vars sections
        if current_group in skip_groups or current_group is None:
            continue

        # Host line
        parts = line.split()
        if not parts:
            continue

        hostname = parts[0]
        ip = None
        for p in parts[1:]:
            if p.startswith('ansible_host='):
                ip = p.split('=', 1)[1]

        if ip:
            targets.append({
                'targets': [f'{ip}:${PORT}'],
                'labels': {'instance': hostname}
            })

print(json.dumps(targets, indent=2))
" > /tmp/vm-targets.json

echo "Generated $(python3 -c "import json; print(len(json.load(open('/tmp/vm-targets.json'))))" ) targets from inventory"

# Apply as ConfigMap
kubectl create configmap "${CONFIGMAP_NAME}" \
  --namespace="${NAMESPACE}" \
  --from-file=vm-targets.json=/tmp/vm-targets.json \
  --dry-run=client -o yaml | kubectl apply -f -

echo "ConfigMap ${CONFIGMAP_NAME} updated in ${NAMESPACE}"
rm /tmp/vm-targets.json
