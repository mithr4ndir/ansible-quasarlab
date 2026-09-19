#!/usr/bin/env bash
#
# Security enforcement loop — runs on a fast cadence (30m) from cmd_center.
# Covers: Wazuh SIEM, CrowdSec, and Prometheus target sync.
#
set -uo pipefail

# Automation checkout, NOT the operator working tree. Force-synced to
# origin/main below so scheduled runs only ever apply merged code.
REPO_DIR="${ANSIBLE_AUTOMATION_REPO_DIR:-/var/lib/ansible-quasarlab/repo}"
LOG_DIR="${ANSIBLE_LOG_DIR:-/var/log/ansible-quasarlab}"
LOGFILE="${LOG_DIR}/security-$(date +%Y%m%d-%H%M%S).log"
TEXTFILE_DIR="${ANSIBLE_TEXTFILE_DIR:-/var/lib/node_exporter/textfiles}"
ARA_ENV_FILE="${ARA_ENV_FILE:-/etc/profile.d/ara-ansible-env.sh}"
PROM_FILE="${TEXTFILE_DIR}/ansible_security.prom"

mkdir -p "$LOG_DIR" "$TEXTFILE_DIR"

# shellcheck source=lib/sync-repo.sh
source "${REPO_DIR}/scripts/lib/sync-repo.sh"

# Publish a repo-sync failure and bail out rather than running playbooks from an
# untrusted tree. See scripts/lib/sync-repo.sh for why this replaced the old
# unchecked `git pull --ff-only`.
write_sync_failure_metric() {
    {
        echo '# HELP ansible_security_run_repo_sync_success Whether the automation checkout synced to its pinned ref (1=success, 0=failure).'
        echo '# TYPE ansible_security_run_repo_sync_success gauge'
        echo "ansible_security_run_repo_sync_success{repo=\"$1\"} 0"
    } > "${PROM_FILE}.tmp"
    mv "${PROM_FILE}.tmp" "$PROM_FILE"
    chmod 644 "$PROM_FILE"
}

if ! sync_repo_to_remote_ref "$REPO_DIR" main >> "$LOGFILE" 2>&1; then
    echo "FATAL: could not pin ${REPO_DIR} to origin/main; refusing to run." | tee -a "$LOGFILE" >&2
    write_sync_failure_metric ansible-quasarlab
    exit 1
fi

# shellcheck source=lib/op-killswitch.sh
source "${REPO_DIR}/scripts/lib/op-killswitch.sh"
# shellcheck source=lib/op-secret-cache.sh
source "${REPO_DIR}/scripts/lib/op-secret-cache.sh"
# shellcheck source=lib/proxmox-vault.sh
source "${REPO_DIR}/scripts/lib/proxmox-vault.sh"
# shellcheck source=lib/ara-run-links.sh
source "${REPO_DIR}/scripts/lib/ara-run-links.sh"
# If 1P is currently rate-limited (known via the shared lock file),
# skip this run entirely so we do not keep the rolling window pinned.
op_killswitch_check_or_exit
# Proactive check: refuse to start when the account quota is already spent,
# rather than waiting to be told "Too many requests" and pinning the window.
op_preflight_check_or_exit

# Source 1Password service account token for the remaining op-cached
# secrets (and for vault-pass.sh's own op read of the vault password).
export OP_SERVICE_ACCOUNT_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-$(cat ~/.config/op/service-account-token 2>/dev/null || true)}"

# Decrypt the Proxmox API token from ansible-vault before any
# inventory resolution. Same pattern as run-proxmox.sh, see issue #124.
if ! load_proxmox_token_from_vault; then
    echo "ERROR: failed to decrypt Proxmox API token from ansible-vault." >&2
    echo "       See docs/vault.md for recovery steps." >&2
    exit 1
fi

# Pre-populate the remaining op-cached secrets. See run-proxmox.sh
# for the rationale. Proxmox token intentionally moved to vault.
load_cached_secrets <<'SECRETS'
WAZUH_PASSWORD                   wazuh_password                      op://Infrastructure/Wazuh SIEM/password
WAZUH_API_PASSWORD               wazuh_api_password                  op://Infrastructure/Wazuh SIEM/API Credentials/api_password
WAZUH_INDEXER_ADMIN_PASSWORD     wazuh_indexer_admin_password        op://Infrastructure/Wazuh SIEM/Indexer/indexer_admin_password
SECRETS

# Source ARA callback plugin environment
if [[ -f "$ARA_ENV_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$ARA_ENV_FILE"
fi

# One ARA label for this run, so the alerts link to the report of the run that
# fired them. See lib/ara-run-links.sh and run-proxmox.sh.
ara_tag_run security

# Resolve inventory with fallback to cache
source "${REPO_DIR}/scripts/resolve-inventory.sh"

start_time=$(date +%s)
exit_code=0
declare -A playbook_results
declare -A playbook_failed_hosts
declare -A playbook_changed_hosts
declare -A playbook_total_changed

for playbook in wazuh.yml crowdsec.yml; do
    echo "=== Running ${playbook} ===" >> "$LOGFILE"
    tmpfile=$(mktemp)
    ansible-playbook "playbooks/${playbook}" $INVENTORY_ARGS --diff > "$tmpfile" 2>&1
    rc=$?
    cat "$tmpfile" >> "$LOGFILE"
    playbook_results["${playbook}"]=$rc
    # A run whose op call hit the rate limit trips the kill switch for
    # everything after it. Only the op error line counts, not the phrase
    # anywhere in the output: --diff text once tripped it (issue #160).
    op_killswitch_scan_playbook_output "$tmpfile" || true

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

# Looked up before the metrics block so a slow or dead ARA cannot interrupt the
# write. Empty on any failure.
ara_links=$(ara_run_link_metrics "${!playbook_results[@]}" 2>> "$LOGFILE")

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
# HELP ansible_security_run_repo_sync_success Whether the automation checkout synced to its pinned ref (1=success, 0=failure).
# TYPE ansible_security_run_repo_sync_success gauge
ansible_security_run_repo_sync_success{repo="ansible-quasarlab"} 1
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

# ARA links, in the same atomic write as everything above. Absent when the
# lookup failed; the alerts then link to the ARA index instead.
[[ -n "$ara_links" ]] && printf '%s\n' "$ara_links"
} > "${PROM_FILE}.tmp"

mv "${PROM_FILE}.tmp" "$PROM_FILE"
chmod 644 "$PROM_FILE"

# Keep only last 50 log files
ls -t "$LOG_DIR"/security-*.log 2>/dev/null | tail -n +51 | xargs -r rm --

exit $exit_code
