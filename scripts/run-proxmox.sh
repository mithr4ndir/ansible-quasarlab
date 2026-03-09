#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="/home/ladino/code/ansible-quasarlab"
OBSERVABILITY_REPO_DIR="/home/ladino/code/observability-quasarlab"
LOG_DIR="/var/log/ansible-quasarlab"
LOGFILE="${LOG_DIR}/ansible-$(date +%Y%m%d-%H%M%S).log"
TEXTFILE_DIR="/var/lib/node_exporter/textfiles"
PROM_FILE="${TEXTFILE_DIR}/ansible_run.prom"

mkdir -p "$LOG_DIR" "$TEXTFILE_DIR"

# Pull latest from both repos
cd "$REPO_DIR"
git pull --ff-only origin main >> "$LOGFILE" 2>&1

cd "$OBSERVABILITY_REPO_DIR"
git pull --ff-only origin master >> "$LOGFILE" 2>&1

cd "$REPO_DIR"

start_time=$(date +%s)
exit_code=0
declare -A playbook_results
declare -A playbook_failed_hosts

# Run playbooks (don't exit on failure — we still need to rotate logs and write metrics)
for playbook in proxmox.yml monitoring.yml grafana_config.yml jellyfin.yml; do
    echo "=== Running ${playbook} ===" >> "$LOGFILE"
    tmpfile=$(mktemp)
    ansible-playbook "playbooks/${playbook}" --diff > "$tmpfile" 2>&1
    rc=$?
    cat "$tmpfile" >> "$LOGFILE"
    playbook_results["${playbook}"]=$rc

    # Parse PLAY RECAP for failed/unreachable hosts
    failed_hosts=""
    if [[ $rc -ne 0 ]]; then
        failed_hosts=$(grep -E '(failed=[1-9]|unreachable=[1-9])' "$tmpfile" \
            | awk '{print $1}' \
            | sort -u \
            | paste -sd ',' -)
    fi
    playbook_failed_hosts["${playbook}"]="${failed_hosts:-unknown}"
    rm -f "$tmpfile"

    if [[ $rc -ne 0 ]]; then
        exit_code=$rc
    fi
done

end_time=$(date +%s)
duration=$(( end_time - start_time ))

# Write Prometheus metrics
if [[ $exit_code -eq 0 ]]; then
    success=1
else
    success=0
fi

{
cat <<METRICS
# HELP ansible_run_success Whether the last ansible timer run succeeded (1=success, 0=failure).
# TYPE ansible_run_success gauge
ansible_run_success ${success}
# HELP ansible_run_timestamp_seconds Unix timestamp of the last ansible timer run completion.
# TYPE ansible_run_timestamp_seconds gauge
ansible_run_timestamp_seconds ${end_time}
# HELP ansible_run_duration_seconds Duration of the last ansible timer run in seconds.
# TYPE ansible_run_duration_seconds gauge
ansible_run_duration_seconds ${duration}
# HELP ansible_playbook_success Whether the last run of each playbook succeeded (1=success, 0=failure).
# TYPE ansible_playbook_success gauge
METRICS

for playbook in "${!playbook_results[@]}"; do
    rc=${playbook_results[$playbook]}
    if [[ $rc -eq 0 ]]; then
        pb_success=1
        hosts="none"
    else
        pb_success=0
        hosts=${playbook_failed_hosts[$playbook]}
    fi
    echo "ansible_playbook_success{playbook=\"${playbook}\",failed_hosts=\"${hosts}\"} ${pb_success}"
done
} > "${PROM_FILE}.tmp"

mv "${PROM_FILE}.tmp" "$PROM_FILE"
chmod 644 "$PROM_FILE"

# Keep only last 50 log files
ls -t "$LOG_DIR"/ansible-*.log 2>/dev/null | tail -n +51 | xargs -r rm --

exit $exit_code
