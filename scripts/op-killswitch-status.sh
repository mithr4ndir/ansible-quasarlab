#!/usr/bin/env bash
# Human-friendly status + manual override for the 1Password rate-limit
# kill switch.
#
#   ./scripts/op-killswitch-status.sh          show current state
#   ./scripts/op-killswitch-status.sh clear    remove the lock (manual)
#   ./scripts/op-killswitch-status.sh trip     set the lock (debug/test)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/op-killswitch.sh
source "${SCRIPT_DIR}/lib/op-killswitch.sh"

cmd="${1:-status}"

case "$cmd" in
    status)
        if op_killswitch_is_active; then
            tripped=$(stat -c %Y "$OP_KILLSWITCH_LOCK" 2>/dev/null || echo 0)
            age=$(( $(date +%s) - tripped ))
            remaining=$(( OP_KILLSWITCH_TTL_SECS - age ))
            echo "Killswitch: ACTIVE"
            echo "  lock:      $OP_KILLSWITCH_LOCK"
            echo "  tripped:   $(date -d @"$tripped" -u +%FT%TZ) ($(( age / 60 ))m ago)"
            echo "  ttl:       ${OP_KILLSWITCH_TTL_SECS}s"
            echo "  expires:   $(date -d @"$(( tripped + OP_KILLSWITCH_TTL_SECS ))" -u +%FT%TZ) (in $(( remaining / 60 ))m)"
            echo "  reason:    $(cat "$OP_KILLSWITCH_LOCK" 2>/dev/null || echo '?')"
            echo
            echo "Clear manually once 1Password rate limit has cleared:"
            echo "  $0 clear"
            exit 1
        else
            echo "Killswitch: inactive (1Password calls allowed)"
        fi
        ;;
    clear|off|unlock)
        if [[ -f "$OP_KILLSWITCH_LOCK" ]]; then
            rm -f "$OP_KILLSWITCH_LOCK"
            op_killswitch_write_metric 0 0
            echo "Killswitch cleared."
        else
            echo "Killswitch was not set; nothing to clear."
        fi
        ;;
    trip|on|lock)
        op_killswitch_trip "manual"
        echo "Killswitch tripped manually."
        ;;
    *)
        echo "Usage: $0 [status|clear|trip]" >&2
        exit 2
        ;;
esac
