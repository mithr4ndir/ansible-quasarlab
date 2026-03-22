#!/usr/bin/env bash
#
# Security enforcement loop — runs on a fast cadence (30m) from cmd_center.
# Covers: Wazuh SIEM, CrowdSec, and Prometheus target sync.
#
set -uo pipefail

REPO_DIR="/home/ladino/code/ansible-quasarlab"
LOG_DIR="/var/log/ansible-quasarlab"
LOGFILE="${LOG_DIR}/security-$(date +%Y%m%d-%H%M%S).log"
TEXTFILE_DIR="/var/lib/node_exporter/textfiles"
PROM_FILE="${TEXTFILE_DIR}/ansible_security.prom"

mkdir -p "$LOG_DIR" "$TEXTFILE_DIR"

# Source 1Password service account token for dynamic inventory + vault
export OP_SERVICE_ACCOUNT_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-$(cat ~/.config/op/service-account-token 2>/dev/null || true)}"
if [[ -n "$OP_SERVICE_ACCOUNT_TOKEN" ]] && command -v op &>/dev/null; then
    export PROXMOX_TOKEN_SECRET="${PROXMOX_TOKEN_SECRET:-$(op read "op://Infrastructure/Proxmox VE API/Ansible Inventory/token_secret" 2>/dev/null || true)}"
fi

# Source ARA callback plugin environment
if [[ -f /etc/profile.d/ara-ansible-env.sh ]]; then
    source /etc/profile.d/ara-ansible-env.sh
fi

# Pull latest
cd "$REPO_DIR"
git pull --ff-only origin main >> "$LOGFILE" 2>&1

start_time=$(date +%s)
exit_code=0
declare -A playbook_results
declare -A playbook_failed_hosts
declare -A playbook_changed_hosts
declare -A playbook_total_changed

for playbook in wazuh.yml crowdsec.yml; do
    echo "=== Running ${playbook} ===" >> "$LOGFILE"
    tmpfile=$(mktemp)
    ansible-playbook "playbooks/${playbook}" --diff > "$tmpfile" 2>&1
    rc=$?
    cat "$tmpfile" >> "$LOGFILE"
    playbook_results["${playbook}"]=$rc

    failed_hosts=""
    if [[ $rc -ne 0 ]]; then
        failed_hosts=$(grep -E '(failed=[1-9]|unreachable=[1-9])' "$tmpfile" \
            | awk '{print $1}' \
            | sort -u \
            | paste -sd ',' -)
    fi
    playbook_failed_hosts["${playbook}"]="${failed_hosts:-unknown}"

    changed_hosts=$(grep -E 'changed=[1-9]' "$tmpfile" \
        | awk '{print $1}' \
        | sort -u \
        | paste -sd ',' -)
    total_changed=$(grep -E 'changed=[0-9]+' "$tmpfile" \
        | grep -oP 'changed=\K[0-9]+' \
        | awk '{sum+=$1} END {print sum+0}')
    playbook_changed_hosts["${playbook}"]="${changed_hosts:-none}"
    playbook_total_changed["${playbook}"]="${total_changed:-0}"

    rm -f "$tmpfile"

    if [[ $rc -ne 0 ]]; then
        exit_code=$rc
    fi
done

# Sync Prometheus targets (adds/removes VMs from scrape config)
echo "=== Syncing Prometheus targets ===" >> "$LOGFILE"
"${REPO_DIR}/scripts/sync-prometheus-targets.sh" >> "$LOGFILE" 2>&1

end_time=$(date +%s)
duration=$(( end_time - start_time ))

if [[ $exit_code -eq 0 ]]; then
    success=1
else
    success=0
fi

{
cat <<METRICS
# HELP ansible_security_run_success Whether the last security timer run succeeded (1=success, 0=failure).
# TYPE ansible_security_run_success gauge
ansible_security_run_success ${success}
# HELP ansible_security_run_timestamp_seconds Unix timestamp of the last security timer run completion.
# TYPE ansible_security_run_timestamp_seconds gauge
ansible_security_run_timestamp_seconds ${end_time}
# HELP ansible_security_run_duration_seconds Duration of the last security timer run in seconds.
# TYPE ansible_security_run_duration_seconds gauge
ansible_security_run_duration_seconds ${duration}
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

cat <<'CHANGED_METRICS'
# HELP ansible_playbook_changed_tasks Total changed tasks in the last run of each playbook.
# TYPE ansible_playbook_changed_tasks gauge
CHANGED_METRICS

for playbook in "${!playbook_total_changed[@]}"; do
    hosts=${playbook_changed_hosts[$playbook]}
    total=${playbook_total_changed[$playbook]}
    echo "ansible_playbook_changed_tasks{playbook=\"${playbook}\",changed_hosts=\"${hosts}\"} ${total}"
done
} > "${PROM_FILE}.tmp"

mv "${PROM_FILE}.tmp" "$PROM_FILE"
chmod 644 "$PROM_FILE"

# Keep only last 50 log files
ls -t "$LOG_DIR"/security-*.log 2>/dev/null | tail -n +51 | xargs -r rm --

exit $exit_code
