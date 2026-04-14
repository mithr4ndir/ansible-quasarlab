#!/bin/bash
# Wait for the TrueNAS API to respond before pve-guests starts its VMs.
#
# The freenas-proxmox plugin calls the TrueNAS REST API at VM start time
# to resolve iSCSI extent paths. If pve-guests fires before the API is
# reachable (common on a cold boot where TrueNAS is still booting up or
# the storage network link is still negotiating), the plugin fails fast
# and onboot VMs stay stopped. See 2026-04-13 incident.
#
# Env vars (set via systemd drop-in, managed by Ansible role):
#   TRUENAS_API_URL       full URL to poll, default https://10.10.12.2/api/v2.0/system/info
#   TRUENAS_API_TIMEOUT   seconds to wait total, default 300 (TrueNAS SCALE
#                         cold boot can take 90-120s on its own, plus storage
#                         VLAN negotiation; 300s covers the joint reboot case
#                         like a power outage where TrueNAS and pve start
#                         around the same time)
#   TRUENAS_API_INTERVAL  seconds between attempts, default 2
#
# Exits 0 on success OR on timeout. We never block pve-guests forever
# (the plugin's own error is more actionable than a hung boot).

set -uo pipefail

API_URL="${TRUENAS_API_URL:-https://10.10.12.2/api/v2.0/system/info}"
TIMEOUT_SECS="${TRUENAS_API_TIMEOUT:-300}"
INTERVAL="${TRUENAS_API_INTERVAL:-2}"

deadline=$(( $(date +%s) + TIMEOUT_SECS ))
attempts=0

while [ "$(date +%s)" -lt "$deadline" ]; do
    attempts=$((attempts + 1))
    # Any 3-digit HTTP response means the API daemon is serving (even 401
    # without auth, which is what an unauthenticated GET returns). We only
    # care that the daemon is up, not that our probe is authorized, so do
    # NOT pass -f.
    http_code=$(curl -ks --max-time 3 -o /dev/null -w '%{http_code}' "$API_URL" 2>/dev/null || true)
    if [[ "$http_code" =~ ^[1-5][0-9]{2}$ ]]; then
        logger -t wait-truenas-api "TrueNAS API at $API_URL reachable (HTTP $http_code) after $attempts attempt(s)"
        exit 0
    fi
    sleep "$INTERVAL"
done

logger -t wait-truenas-api "TIMEOUT after ${TIMEOUT_SECS}s and $attempts attempt(s) waiting for $API_URL; letting pve-guests proceed"
exit 0
