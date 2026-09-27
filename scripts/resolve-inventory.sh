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

    # Dynamic inventory works, so cache a static snapshot for fallback.
    #
    # The snapshot is MERGED with the previous cache rather than replacing it.
    # `ansible_host` is composed from proxmox_agent_interfaces, so a guest agent
    # that misses one reply leaves it undefined for that host. This lab has no
    # DNS (pfSense returns NXDOMAIN for every lab name), so Ansible then falls
    # back to the inventory hostname and the host is simply unreachable. The old
    # writer made that permanent by rewriting the cache entry without an address.
    cd "$REPO_DIR" || return 1
    ansible-inventory --list 2>/dev/null | python3 -c "
import json, os, re, sys

cache_path = sys.argv[1] if len(sys.argv) > 1 else ''

# Last known good address per host, from the previous cache.
prev = {}
if cache_path and os.path.exists(cache_path):
    with open(cache_path) as fh:
        for line in fh:
            m = re.match(r'^(\S+)\s+ansible_host=(\S+)', line)
            if m:
                prev[m.group(1)] = m.group(2)

data = json.load(sys.stdin)
hostvars = data.get('_meta', {}).get('hostvars', {})

groups = {}
for group_name, group_data in data.items():
    if group_name == '_meta':
        continue
    hosts = group_data.get('hosts', [])
    if hosts:
        groups[group_name] = hosts

degraded = []   # agent reported no address this cycle, recovered from cache
dropped = []    # no address anywhere, omitted rather than written addressless
resolved = {}

for host in sorted({h for hs in groups.values() for h in hs}):
    hv = hostvars.get(host, {})
    ip = hv.get('ansible_host', '')
    if not ip:
        ip = prev.get(host, '')
        if ip:
            degraded.append(host)
        else:
            dropped.append(host)
            continue
    resolved[host] = (ip, hv.get('ansible_user', 'ladino'))

for group_name in sorted(groups.keys()):
    if group_name.startswith('proxmox_'):
        continue
    print(f'[{group_name}]')
    for host in sorted(groups[group_name]):
        if host in resolved:
            ip, user = resolved[host]
            print(f'{host} ansible_host={ip} ansible_user={user}')
    print()

print('[linux:children]')
for group_name in sorted(groups.keys()):
    if group_name in ('linux', 'all', 'ungrouped') or group_name.startswith('proxmox_'):
        continue
    linux_hosts = set(groups.get('linux', []))
    group_hosts = set(groups[group_name])
    if group_hosts and group_hosts.issubset(linux_hosts):
        print(group_name)
print()

if degraded:
    sys.stderr.write('DEGRADED ' + ','.join(degraded) + chr(10))
if dropped:
    sys.stderr.write('DROPPED ' + ','.join(dropped) + chr(10))
" "$INVENTORY_CACHE" > "${INVENTORY_CACHE}.tmp" 2>"${INVENTORY_CACHE}.status"

    if [[ -s "${INVENTORY_CACHE}.tmp" ]]; then
        mv "${INVENTORY_CACHE}.tmp" "$INVENTORY_CACHE"
        chmod 644 "$INVENTORY_CACHE"
        host_count=$(grep 'ansible_host' "$INVENTORY_CACHE" | awk '{print $1}' | sort -u | wc -l)
        echo "$(date -Iseconds) Inventory cache updated (${host_count} unique hosts)" >> "$LOGFILE"

        # Hosts whose address had to come from the cache, or that have none at
        # all. Set INVENTORY_DEGRADED so the caller can prefer the merged cache
        # over a live inventory that would address them by name.
        INVENTORY_DEGRADED=""
        if [[ -s "${INVENTORY_CACHE}.status" ]]; then
            while read -r kind hosts; do
                case "$kind" in
                    DEGRADED)
                        INVENTORY_DEGRADED="$hosts"
                        echo "$(date -Iseconds) WARNING: guest agent reported no address for ${hosts}; using last known good address from cache" >> "$LOGFILE"
                        ;;
                    DROPPED)
                        INVENTORY_DEGRADED="${INVENTORY_DEGRADED:+$INVENTORY_DEGRADED,}$hosts"
                        echo "$(date -Iseconds) ERROR: no address for ${hosts} from the agent or the cache; omitted from inventory rather than addressed by hostname (this lab has no DNS)" >> "$LOGFILE"
                        ;;
                esac
            done < "${INVENTORY_CACHE}.status"
        fi
        rm -f "${INVENTORY_CACHE}.status"
        return 0
    else
        rm -f "${INVENTORY_CACHE}.tmp" "${INVENTORY_CACHE}.status"
        return 1
    fi
}

# Main logic
INVENTORY_ARGS=""

if _resolve_dynamic_inventory; then
    if [[ -n "${INVENTORY_DEGRADED:-}" ]]; then
        # The API answered, but at least one host came back without an address.
        # Using the live inventory would address that host by hostname, which
        # cannot resolve here, so prefer the cache we just merged: it carries
        # the last known good address for exactly those hosts.
        echo "$(date -Iseconds) WARNING: live inventory incomplete (${INVENTORY_DEGRADED}), using merged cache instead" >> "$LOGFILE"
        INVENTORY_ARGS="-i $INVENTORY_CACHE"
    else
        # Dynamic inventory works, use it normally (no extra args needed)
        INVENTORY_ARGS=""
    fi
else
    # Dynamic inventory failed, fall back to cache
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
