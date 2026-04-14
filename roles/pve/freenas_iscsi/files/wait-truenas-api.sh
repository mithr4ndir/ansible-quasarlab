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
#   TRUENAS_API_TIMEOUT   seconds to wait total, default 120
#   TRUENAS_API_INTERVAL  seconds between attempts, default 2
#
# Exits 0 on success OR on timeout. We never block pve-guests forever
# (the plugin's own error is more actionable than a hung boot).

set -u

API_URL="${TRUENAS_API_URL:-https://10.10.12.2/api/v2.0/system/info}"
TIMEOUT_SECS="${TRUENAS_API_TIMEOUT:-120}"
INTERVAL="${TRUENAS_API_INTERVAL:-2}"

deadline=$(( $(date +%s) + TIMEOUT_SECS ))
attempts=0

while [ "$(date +%s)" -lt "$deadline" ]; do
    attempts=$((attempts + 1))
    if curl -ksfL --max-time 3 -o /dev/null "$API_URL"; then
        logger -t wait-truenas-api "TrueNAS API at $API_URL reachable after $attempts attempt(s)"
        exit 0
    fi
    sleep "$INTERVAL"
done

logger -t wait-truenas-api "TIMEOUT after ${TIMEOUT_SECS}s and $attempts attempt(s) waiting for $API_URL; letting pve-guests proceed"
exit 0
