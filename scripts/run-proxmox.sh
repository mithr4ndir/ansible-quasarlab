#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="/home/ladino/code/ansible-quasarlab"
OBSERVABILITY_REPO_DIR="/home/ladino/code/observability-quasarlab"
LOG_DIR="/var/log/ansible-quasarlab"
LOGFILE="${LOG_DIR}/ansible-$(date +%Y%m%d-%H%M%S).log"
TEXTFILE_DIR="/var/lib/node_exporter/textfiles"
PROM_FILE="${TEXTFILE_DIR}/ansible_run.prom"

mkdir -p "$LOG_DIR" "$TEXTFILE_DIR"

# shellcheck source=lib/op-killswitch.sh
source "${REPO_DIR}/scripts/lib/op-killswitch.sh"
# If 1P is currently rate-limited (known via the shared lock file),
# skip this run entirely so we do not keep the rolling window pinned.
op_killswitch_check_or_exit

# Source 1Password service account token for dynamic inventory + vault
export OP_SERVICE_ACCOUNT_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-$(cat ~/.config/op/service-account-token 2>/dev/null || true)}"
if [[ -n "$OP_SERVICE_ACCOUNT_TOKEN" ]] && command -v op &>/dev/null; then
    op_err=$(mktemp)
    token_value=$(op read "op://Infrastructure/Proxmox API/Ansible Inventory/token_secret" 2>"$op_err" || true)
    if [[ -n "$token_value" ]]; then
        export PROXMOX_TOKEN_SECRET="${PROXMOX_TOKEN_SECRET:-$token_value}"
    else
        op_killswitch_scan_file "$op_err" || true
    fi
    rm -f "$op_err"
fi

# Source ARA callback plugin environment (records runs to ARA database)
if [[ -f /etc/profile.d/ara-ansible-env.sh ]]; then
    source /etc/profile.d/ara-ansible-env.sh
fi

# Pull latest from both repos
cd "$REPO_DIR"
git pull --ff-only origin main >> "$LOGFILE" 2>&1

cd "$OBSERVABILITY_REPO_DIR"
git pull --ff-only origin master >> "$LOGFILE" 2>&1

cd "$REPO_DIR"

# Resolve inventory with fallback to cache
source "${REPO_DIR}/scripts/resolve-inventory.sh"

start_time=$(date +%s)
exit_code=0
declare -A playbook_results
declare -A playbook_failed_hosts
declare -A playbook_changed_hosts
declare -A playbook_total_changed

# Run playbooks (don't exit on failure — we still need to rotate logs and write metrics)
for playbook in proxmox.yml vm_baseline.yml monitoring.yml grafana_config.yml jellyfin.yml authentik.yml lb_setup.yml deploy-ha.yml; do
    echo "=== Running ${playbook} ===" >> "$LOGFILE"
    tmpfile=$(mktemp)
    ansible-playbook "playbooks/${playbook}" $INVENTORY_ARGS --diff > "$tmpfile" 2>&1
    rc=$?
    cat "$tmpfile" >> "$LOGFILE"
    playbook_results["${playbook}"]=$rc
    # Any playbook that invoked `op read` and hit the rate limit puts
    # "Too many requests" in its output. Surface that to the killswitch
    # so subsequent scheduled runs short-circuit.
    op_killswitch_scan_file "$tmpfile" || true

    # Parse PLAY RECAP for failed/unreachable hosts
    failed_hosts=""
    if [[ $rc -ne 0 ]]; then
        failed_hosts=$(grep -E '(failed=[1-9]|unreachable=[1-9])' "$tmpfile" \
            | awk '{print $1}' \
            | sort -u \
            | paste -sd ',' -)
    fi
    playbook_failed_hosts["${playbook}"]="${failed_hosts:-unknown}"

    # Parse PLAY RECAP for changed hosts
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
ls -t "$LOG_DIR"/ansible-*.log 2>/dev/null | tail -n +51 | xargs -r rm --

exit $exit_code
