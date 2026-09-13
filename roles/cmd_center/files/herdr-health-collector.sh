#!/bin/bash
# Herdr health collector for the node_exporter textfile collector.
#
# node_exporter's systemd collector only reads the system manager, so it
# cannot see the herdr.service user unit. This script runs from a systemd
# user timer, asks the user manager for the unit state, and writes gauges
# shaped like node_systemd_unit_state to a .prom file.
#
# Coverage split: if the user manager itself is down, this timer cannot
# fire either. That case shows up as a stale
# herdr_health_collector_timestamp_seconds, and directly through
# user@1000.service in node_exporter's systemd allowlist.
#
# The state string from systemctl is compared against a fixed list and is
# never written into the output, so nothing it prints can inject labels.
set -uo pipefail

PROM_FILE="${HERDR_HEALTH_PROM_FILE:-/var/lib/node_exporter/textfiles/herdr.prom}"
UNIT="${HERDR_HEALTH_UNIT:-herdr.service}"
LOG_TAG="herdr-health-collector"

# UNIT lands in a label value, so hold it to unit-name characters.
if [[ ! "$UNIT" =~ ^[A-Za-z0-9@._-]+$ ]]; then
    logger -t "$LOG_TAG" "reason=invalid_unit_name"
    exit 1
fi

# Same state set node_exporter exports for system units.
STATES=(activating active deactivating failed inactive)
# Other states systemd can legitimately report. Recognised, so the
# collector still counts as successful, but not broken out as a series.
OTHER_STATES=(reloading refreshing maintenance)

# is-active exits non-zero for every state except active, so its exit
# status is not an error signal. An empty stdout is: it means the user
# manager could not be reached.
state="$(systemctl --user is-active "$UNIT" 2>/dev/null)"

success=0
for known in "${STATES[@]}" "${OTHER_STATES[@]}"; do
    if [[ "$state" == "$known" ]]; then
        success=1
        break
    fi
done

prom_dir="$(dirname "$PROM_FILE")"
# Temp file in the same directory so the final mv is an atomic rename and
# node_exporter never scrapes a half-written file. The leading dot and
# random suffix keep it out of node_exporter's *.prom glob.
if ! tmp="$(mktemp "${prom_dir}/.herdr.prom.XXXXXX" 2>/dev/null)"; then
    logger -t "$LOG_TAG" "reason=tempfile_failed dir=${prom_dir}"
    exit 1
fi
trap 'rm -f "$tmp"' EXIT

{
    echo "# HELP herdr_systemd_unit_state Herdr user unit state, 1 for the current state."
    echo "# TYPE herdr_systemd_unit_state gauge"
    for s in "${STATES[@]}"; do
        value=0
        if [[ "$success" -eq 1 && "$state" == "$s" ]]; then
            value=1
        fi
        echo "herdr_systemd_unit_state{name=\"${UNIT}\",state=\"${s}\"} ${value}"
    done
    echo "# HELP herdr_health_collector_success 1 if the user manager returned a recognised unit state."
    echo "# TYPE herdr_health_collector_success gauge"
    echo "herdr_health_collector_success ${success}"
    echo "# HELP herdr_health_collector_timestamp_seconds Unix time of the last collector run."
    echo "# TYPE herdr_health_collector_timestamp_seconds gauge"
    echo "herdr_health_collector_timestamp_seconds $(date +%s)"
} > "$tmp"

# mktemp creates 0600; node_exporter runs as its own user and must read it.
chmod 0644 "$tmp"
if ! mv "$tmp" "$PROM_FILE"; then
    logger -t "$LOG_TAG" "reason=write_failed file=${PROM_FILE}"
    exit 1
fi
trap - EXIT

if [[ "$success" -ne 1 ]]; then
    logger -t "$LOG_TAG" "reason=unrecognised_state unit=${UNIT}"
fi
exit 0
