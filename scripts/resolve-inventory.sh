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

# Per-process scratch paths. The proxmox and security timers run as the same
# user and can overlap (OnUnitActiveSec drift, or one run outliving its slot),
# so a shared status file lets a healthy run truncate the DEGRADED marker a
# concurrent degraded run just wrote. That run would then conclude the
# inventory was complete and proceed with the addressless one, which is the
# exact failure this file exists to prevent. Same reasoning for .tmp staging.
_INV_TMP="${INVENTORY_CACHE}.tmp.$$"
_INV_STATUS="${INVENTORY_CACHE}.status.$$"

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
children = {}
for group_name, group_data in data.items():
    if group_name == '_meta':
        continue
    hosts = group_data.get('hosts', [])
    kids = [c for c in group_data.get('children', []) if c not in ('ungrouped',)]
    if hosts:
        groups[group_name] = hosts
    if kids:
        children[group_name] = kids


def group_hosts_recursive(name, _seen=None):
    # Every host under a group, following children.
    _seen = _seen or set()
    if name in _seen:
        return set()
    _seen.add(name)
    out = set(groups.get(name, []))
    for kid in children.get(name, []):
        out |= group_hosts_recursive(kid, _seen)
    return out

degraded = []    # had an address last cycle and lost it: worth falling back for
no_address = []  # never had one (stopped/agentless): expected, not a fault
resolved = {}

for host in sorted({h for hs in groups.values() for h in hs}):
    hv = hostvars.get(host, {})
    ip = hv.get('ansible_host', '')
    if not ip:
        ip = prev.get(host, '')
        if ip:
            # Had an address last cycle and lost it: genuinely degraded.
            degraded.append(host)
        else:
            # Never had one. A stopped or agentless VM (templates, Windows
            # guests, powered-off hosts) has no guest agent and so no address,
            # permanently and correctly. Treating that as an
            # incomplete inventory fired the cache fallback on EVERY run, which then
            # exposed the children bug below and silently dropped truenas,
            # pve, pve2 and uptime-kuma out of the linux group.
            no_address.append(host)
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

# Reproduce the real parent/child structure. The previous version guessed it
# with group_hosts.issubset(groups[linux]), but that holds only
# the hosts listed directly under linux, never those reached through its
# children. So nas, proxmox and uptime_kuma (which exist ONLY as
# [linux:children] entries in inventory.static.ini) failed the subset test
# and were left out, putting truenas, pve, pve2 and uptime-kuma in no managed
# group at all. Every hosts: linux play then skipped them silently.
for parent in sorted(children.keys()):
    if parent in ('all', 'ungrouped') or parent.startswith('proxmox_'):
        continue
    kids = [k for k in sorted(children[parent])
            if not k.startswith('proxmox_')
            and (k in groups or k in children)]
    if not kids:
        continue
    print(f'[{parent}:children]')
    for kid in kids:
        print(kid)
    print()

if degraded:
    sys.stderr.write('DEGRADED ' + ','.join(degraded) + chr(10))
if no_address:
    sys.stderr.write('NOADDR ' + ','.join(no_address) + chr(10))
" "$INVENTORY_CACHE" > "$_INV_TMP" 2>"$_INV_STATUS"

    if [[ -s "$_INV_TMP" ]]; then
        mv "$_INV_TMP" "$INVENTORY_CACHE"
        chmod 644 "$INVENTORY_CACHE"
        host_count=$(grep 'ansible_host' "$INVENTORY_CACHE" | awk '{print $1}' | sort -u | wc -l)
        echo "$(date -Iseconds) Inventory cache updated (${host_count} unique hosts)" >> "$LOGFILE"

        # Hosts whose address had to come from the cache, or that have none at
        # all. Set INVENTORY_DEGRADED so the caller can prefer the merged cache
        # over a live inventory that would address them by name.
        INVENTORY_DEGRADED=""
        if [[ -s "$_INV_STATUS" ]]; then
            while read -r kind hosts; do
                case "$kind" in
                    DEGRADED)
                        INVENTORY_DEGRADED="$hosts"
                        echo "$(date -Iseconds) WARNING: guest agent reported no address for ${hosts}; using last known good address from cache" >> "$LOGFILE"
                        ;;
                    NOADDR)
                        # Expected, not a fault: a stopped or agentless guest
                        # has no address and never had one. Logged so it stays
                        # visible, but deliberately NOT counted as degraded,
                        # because doing so fired the fallback on every run.
                        echo "$(date -Iseconds) INFO: no address for ${hosts} (stopped or agentless); omitted from inventory rather than addressed by hostname, this lab has no DNS" >> "$LOGFILE"
                        ;;
                esac
            done < "$_INV_STATUS"
        fi
        rm -f "$_INV_STATUS"
        return 0
    else
        rm -f "$_INV_TMP" "$_INV_STATUS"
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
