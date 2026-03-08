#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="/home/ladino/code/ansible-quasarlab"
LOG_DIR="/var/log/ansible-quasarlab"
LOGFILE="${LOG_DIR}/ansible-$(date +%Y%m%d-%H%M%S).log"
TEXTFILE_DIR="/var/lib/node_exporter/textfiles"
PROM_FILE="${TEXTFILE_DIR}/ansible_run.prom"

mkdir -p "$LOG_DIR" "$TEXTFILE_DIR"

cd "$REPO_DIR"

# Pull latest
git pull --ff-only origin main >> "$LOGFILE" 2>&1

start_time=$(date +%s)
exit_code=0
failed_playbooks=""

# Run playbooks (don't exit on failure — we still need to rotate logs and write metrics)
for playbook in proxmox.yml monitoring.yml; do
    echo "=== Running ${playbook} ===" >> "$LOGFILE"
    ansible-playbook "playbooks/${playbook}" --diff >> "$LOGFILE" 2>&1
    rc=$?
    if [[ $rc -ne 0 ]]; then
        exit_code=$rc
        failed_playbooks="${failed_playbooks} ${playbook}"
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

cat > "${PROM_FILE}.tmp" <<METRICS
# HELP ansible_run_success Whether the last ansible timer run succeeded (1=success, 0=failure).
# TYPE ansible_run_success gauge
ansible_run_success ${success}
# HELP ansible_run_timestamp_seconds Unix timestamp of the last ansible timer run completion.
# TYPE ansible_run_timestamp_seconds gauge
ansible_run_timestamp_seconds ${end_time}
# HELP ansible_run_duration_seconds Duration of the last ansible timer run in seconds.
# TYPE ansible_run_duration_seconds gauge
ansible_run_duration_seconds ${duration}
METRICS

mv "${PROM_FILE}.tmp" "$PROM_FILE"
chmod 644 "$PROM_FILE"

# Keep only last 50 log files
ls -t "$LOG_DIR"/ansible-*.log 2>/dev/null | tail -n +51 | xargs -r rm --

exit $exit_code
