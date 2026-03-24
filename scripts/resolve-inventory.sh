#!/usr/bin/env bash
#
# Shared helper: resolve Proxmox dynamic inventory with fallback to cached snapshot.
# Source this from run-*.sh scripts AFTER setting OP_SERVICE_ACCOUNT_TOKEN.
#
# On success: caches a fresh inventory snapshot to INVENTORY_CACHE.
# On failure: falls back to the cached snapshot and logs a warning.
#
# Exports: INVENTORY_ARGS (pass to ansible-playbook as extra args)
#

# Cache lives in repo root so group_vars/ and host_vars/ are found when falling back
INVENTORY_CACHE="${REPO_DIR}/inventory-cache.ini"

# Try to resolve dynamic inventory and cache the result
_resolve_dynamic_inventory() {
    if [[ -z "${PROXMOX_TOKEN_SECRET:-}" ]]; then
        return 1
    fi

    # Test that the Proxmox API is reachable
    local http_code
    http_code=$(curl -sk -o /dev/null -w "%{http_code}" --connect-timeout 5 \
        -H "Authorization: PVEAPIToken=ansible@pve!inventory=${PROXMOX_TOKEN_SECRET}" \
        https://192.168.1.11:8006/api2/json/nodes 2>/dev/null)

    if [[ "$http_code" != "200" ]]; then
        return 1
    fi

    # Dynamic inventory works — cache a static snapshot for fallback
    cd "$REPO_DIR"
    ansible-inventory --list 2>/dev/null | python3 -c "
import json, sys

data = json.load(sys.stdin)
hostvars = data.get('_meta', {}).get('hostvars', {})

# Build group->hosts mapping
groups = {}
for group_name, group_data in data.items():
    if group_name == '_meta':
        continue
    hosts = group_data.get('hosts', [])
    children = group_data.get('children', [])
    if hosts:
        groups[group_name] = hosts

# Write INI-style inventory
for group_name in sorted(groups.keys()):
    # Skip proxmox auto-generated groups
    if group_name.startswith('proxmox_'):
        continue
    print(f'[{group_name}]')
    for host in sorted(groups[group_name]):
        hv = hostvars.get(host, {})
        ip = hv.get('ansible_host', '')
        user = hv.get('ansible_user', 'ladino')
        if ip:
            print(f'{host} ansible_host={ip} ansible_user={user}')
        else:
            print(f'{host} ansible_user={user}')
    print()

# Write parent group relationships
print('[linux:children]')
for group_name in sorted(groups.keys()):
    if group_name in ('linux', 'all', 'ungrouped') or group_name.startswith('proxmox_'):
        continue
    # Check if this group's hosts are a subset of linux
    linux_hosts = set(groups.get('linux', []))
    group_hosts = set(groups[group_name])
    if group_hosts and group_hosts.issubset(linux_hosts):
        print(group_name)
print()
" > "${INVENTORY_CACHE}.tmp" 2>/dev/null

    if [[ -s "${INVENTORY_CACHE}.tmp" ]]; then
        mv "${INVENTORY_CACHE}.tmp" "$INVENTORY_CACHE"
        chmod 644 "$INVENTORY_CACHE"
        host_count=$(grep 'ansible_host' "$INVENTORY_CACHE" | awk '{print $1}' | sort -u | wc -l)
        echo "$(date -Iseconds) Inventory cache updated (${host_count} unique hosts)" >> "$LOGFILE"
        return 0
    else
        rm -f "${INVENTORY_CACHE}.tmp"
        return 1
    fi
}

# Main logic
INVENTORY_ARGS=""

if _resolve_dynamic_inventory; then
    # Dynamic inventory works — use it normally (no extra args needed)
    INVENTORY_ARGS=""
else
    # Dynamic inventory failed — fall back to cache
    if [[ -f "$INVENTORY_CACHE" ]]; then
        cache_age=$(( $(date +%s) - $(stat -c %Y "$INVENTORY_CACHE" 2>/dev/null || echo 0) ))
        cache_age_human=$(printf '%dd %dh' $((cache_age/86400)) $((cache_age%86400/3600)))
        echo "$(date -Iseconds) WARNING: Proxmox API unreachable, using cached inventory (age: ${cache_age_human})" >> "$LOGFILE"
        INVENTORY_ARGS="-i $INVENTORY_CACHE"
    else
        echo "$(date -Iseconds) ERROR: Proxmox API unreachable and no inventory cache exists. Only static hosts will be targeted." >> "$LOGFILE"
        INVENTORY_ARGS=""
    fi
fi

export INVENTORY_ARGS
