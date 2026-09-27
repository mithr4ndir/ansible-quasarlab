#!/usr/bin/env bash
#
# Generate Prometheus file_sd targets from Ansible inventory and apply
# as a ConfigMap to the K8s cluster. Works with both static and dynamic
# inventory sources.
#
# Usage: ./scripts/sync-prometheus-targets.sh
#
set -euo pipefail

LOG_PREFIX="[sync-prometheus-targets]"
MIN_EXPECTED_TARGETS=10

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/op-killswitch.sh
source "${SCRIPT_DIR}/lib/op-killswitch.sh"
# shellcheck source=lib/proxmox-vault.sh
source "${SCRIPT_DIR}/lib/proxmox-vault.sh"
# Killswitch still gates the run because vault-pass.sh may itself
# call op for the vault password. If the killswitch is tripped, skip
# the run, dynamic inventory falls back to the cached Proxmox snapshot
# so Prometheus targets stay roughly correct while we wait out the window.
op_killswitch_check_or_exit

# Decrypt the Proxmox API token from ansible-vault. Replaces the
# previous direct `op read` here, which was the second-largest 1P
# rate-limit consumer in the repo (issue #124).
if ! load_proxmox_token_from_vault; then
    echo "${LOG_PREFIX} ERROR: failed to decrypt Proxmox token from ansible-vault, only static inventory hosts will be discovered" >&2
fi

NAMESPACE="monitoring"
CONFIGMAP_NAME="prometheus-file-sd-targets"
PORT="9100"

cd "$(dirname "$0")/.."

# Capture stderr from ansible-inventory so plugin errors are visible
inventory_stderr=$(mktemp)
ansible-inventory --list 2>"$inventory_stderr" | python3 -c "
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

if [[ -s "$inventory_stderr" ]]; then
    echo "${LOG_PREFIX} ansible-inventory stderr:" >&2
    sed "s/^/  /" "$inventory_stderr" >&2
fi
rm -f "$inventory_stderr"

count=$(python3 -c "import json; print(len(json.load(open('/tmp/vm-targets.json'))))")
echo "${LOG_PREFIX} Generated ${count} targets from inventory"

if [[ "$count" -lt "$MIN_EXPECTED_TARGETS" ]]; then
    echo "${LOG_PREFIX} ERROR: Only ${count} targets found (expected >= ${MIN_EXPECTED_TARGETS}). Dynamic inventory likely failed — skipping ConfigMap update to avoid overwriting good data" >&2
    rm -f /tmp/vm-targets.json
    exit 1
fi

# Apply as ConfigMap.
#
# Retried because `kubectl apply` validates client-side, which downloads the
# OpenAPI schema from the API server first. A transient API-server hiccup makes
# that download fail with
#   error validating "STDIN": failed to download openapi: unknown
# and takes the whole monitoring.yml run down with it, even though the targets
# were generated correctly and the guard above already passed. Observed
# 2026-09-20T23:02Z, while the cluster was otherwise healthy.
#
# Retry rather than --validate=false: the validation is worth keeping, the
# flakiness is not. Server-side apply would also sidestep the download, but it
# changes field-ownership semantics on a ConfigMap this script is the sole
# writer of, which is a bigger change than the bug warrants.
APPLY_ATTEMPTS=3

apply_targets() {
    kubectl create configmap "${CONFIGMAP_NAME}" \
      --namespace="${NAMESPACE}" \
      --from-file=vm-targets.json=/tmp/vm-targets.json \
      --dry-run=client -o yaml | kubectl apply -f -
}

for attempt in $(seq 1 "${APPLY_ATTEMPTS}"); do
    if apply_targets; then
        break
    fi

    if [[ "${attempt}" -ge "${APPLY_ATTEMPTS}" ]]; then
        echo "${LOG_PREFIX} ERROR: kubectl apply failed ${APPLY_ATTEMPTS} times, giving up. The existing ConfigMap is left untouched." >&2
        rm -f /tmp/vm-targets.json
        exit 1
    fi

    backoff=$((attempt * 5))
    echo "${LOG_PREFIX} kubectl apply failed (attempt ${attempt}/${APPLY_ATTEMPTS}), retrying in ${backoff}s" >&2
    sleep "${backoff}"
done

echo "${LOG_PREFIX} ConfigMap ${CONFIGMAP_NAME} updated in ${NAMESPACE}"
rm /tmp/vm-targets.json
