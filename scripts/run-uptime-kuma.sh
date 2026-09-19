#!/usr/bin/env bash
#
# Operator-run wrapper for playbooks/uptime-kuma.yml. Not on a timer.
#
# Deploys Uptime Kuma, its monitors and the NFS read probe to vm117. The play
# needs the Discord #alerts webhook, which Kuma posts to directly (not through
# the in-cluster discord-alert-proxy, since the cluster is what Kuma watches).
# This resolves it through cached_op_read with the guard rails the other
# wrappers have: merged code only (automation checkout pinned to origin/main),
# quota pre-flight, a log file, and a failure that is reported rather than
# swallowed.
#
# Inventory is inventory.static.ini ONLY. vm117 lives there so that the
# monitor of last resort can be deployed or repaired with the Proxmox API,
# the dynamic inventory and the Proxmox token all unavailable. The vault
# password is still needed (group_vars/all/vault.yml loads for every host);
# vault-pass.sh serves it from the same cache.
#
# Usage:
#   scripts/run-uptime-kuma.sh [ansible-playbook args...]
#
#   Only these ansible-playbook options are passed through, spelled exactly:
#     -C/--check  -D/--diff  --step  --syntax-check  --list-tasks  --list-tags
#     -v ... -vvvvv/--verbose
#     -t/--tags X  --skip-tags X  --start-at-task X  -e/--extra-vars X
#   (value options also as --opt=X). Anything else is refused with exit 2:
#   the inventory is always inventory.static.ini, there is no --limit, and
#   no second playbook. An allowlist, not a denylist, because ansible-playbook
#   accepts abbreviations (--lim, --inventory-f) and clustered short flags
#   (-vi x.yml, -Dlh), which a denylist keeps missing.
#
# Webhook resolution, first match wins:
#   1. UPTIME_KUMA_DISCORD_WEBHOOK_URL already set: used as is. No op call,
#      not cached.
#   2. cached_op_read discord_alerts_webhook_url. A fresh cache makes no op
#      call. A stale one is refreshed if 1Password answers, and served as is
#      when op fails, is rate limited, or the kill switch is active.
#   3. Nothing cached and no live read possible: exit 1 before the host is
#      contacted. A running Kuma keeps its current notification settings. To
#      proceed with 1Password unavailable, export UPTIME_KUMA_DISCORD_WEBHOOK_URL
#      (the #alerts webhook, 1Password item vausmfy2q2m57r6scvziyrc7lq, field
#      credential) and rerun.
#
# The kill switch does not stop this wrapper, because serving cached secrets is
# exactly what it is for. The quota pre-flight does, same as every playbook via
# callback_plugins/op_quota_gate.py. OP_QUOTA_GATE_BYPASS=1 skips both, and with
# a warm cache the run then makes no billable 1Password call.
#
# Exit status: the ansible-playbook exit code; 0 when the quota pre-flight
# skipped the run (it says so on stderr); 1 on a setup failure, including no
# webhook; 2 on bad arguments.
#
set -uo pipefail

# Automation checkout, NOT the operator working tree. Force-synced to
# origin/main below so a manual run applies merged code, same as the timers.
REPO_DIR="${ANSIBLE_AUTOMATION_REPO_DIR:-/var/lib/ansible-quasarlab/repo}"
LOG_DIR="${ANSIBLE_LOG_DIR:-/var/log/ansible-quasarlab}"
LOGFILE="${LOG_DIR}/uptime-kuma-$(date +%Y%m%d-%H%M%S).log"
ARA_ENV_FILE="${ARA_ENV_FILE:-/etc/profile.d/ara-ansible-env.sh}"
PLAYBOOK="uptime-kuma.yml"
INVENTORY="inventory.static.ini"
WEBHOOK_SECRET_SLUG="discord_alerts_webhook_url"
WEBHOOK_SECRET_OP_PATH="op://Infrastructure/vausmfy2q2m57r6scvziyrc7lq/credential"

usage() {
    sed -n '/^# Usage:/,/^# Webhook resolution/{/^# Webhook resolution/d;s/^# \{0,1\}//;p;}' "$0" >&2
    exit 2
}

# --- Arguments ---------------------------------------------------------------
# SECURITY: allowlist. Every argument must be one of the exact spellings
# below; a value option consumes the next argument, which must not itself
# look like an option.
refuse() {
    echo "ERROR: ${1//[^A-Za-z0-9_.=:@\/-]/?} is not allowed. $2" >&2
    echo "       Allowed: --check --diff --step --syntax-check --list-tasks --list-tags -v... --tags --skip-tags --start-at-task --extra-vars (see --help)." >&2
    exit 2
}
passthrough=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            ;;
        -C|--check|-D|--diff|--step|--syntax-check|--list-tasks|--list-tags|--verbose|-v|-vv|-vvv|-vvvv|-vvvvv)
            passthrough+=("$1")
            shift
            ;;
        -t|--tags|--skip-tags|--start-at-task|-e|--extra-vars)
            [[ $# -ge 2 ]] || refuse "$1" "It needs a value."
            [[ "$2" == -* ]] && refuse "$1 $2" "A value may not start with '-'."
            passthrough+=("$1" "$2")
            shift 2
            ;;
        --tags=?*|--skip-tags=?*|--start-at-task=?*|--extra-vars=?*)
            passthrough+=("$1")
            shift
            ;;
        *)
            refuse "$1" "The inventory is always ${INVENTORY}, there is no --limit, and no other playbook."
            ;;
    esac
done

# Running as root would leave root-owned files in the automation checkout and
# the secret cache, breaking the timers that run as the ansible user.
if [[ $EUID -eq 0 ]]; then
    echo "ERROR: run as the ansible user, not root." >&2
    exit 2
fi

mkdir -p "$LOG_DIR" || { echo "ERROR: cannot create ${LOG_DIR}" >&2; exit 1; }

# shellcheck source=lib/sync-repo.sh
source "${REPO_DIR}/scripts/lib/sync-repo.sh"

# A failed sync must never fall through to running from an untrusted tree.
if ! sync_repo_to_remote_ref "$REPO_DIR" main >> "$LOGFILE" 2>&1; then
    echo "FATAL: could not pin ${REPO_DIR} to origin/main; refusing to run." | tee -a "$LOGFILE" >&2
    exit 1
fi

# shellcheck source=lib/op-killswitch.sh
source "${REPO_DIR}/scripts/lib/op-killswitch.sh"
# shellcheck source=lib/op-secret-cache.sh
source "${REPO_DIR}/scripts/lib/op-secret-cache.sh"

# Exported before the pre-flight, which reads the quota with `op service-account
# ratelimit` and fails open without a token. Reading the file costs no op call.
export OP_SERVICE_ACCOUNT_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-$(cat ~/.config/op/service-account-token 2>/dev/null || true)}"

op_preflight_check_or_exit

# Informational only; see the header for why the switch does not stop this run.
if op_killswitch_is_active; then
    echo "1Password kill switch is active (${OP_KILLSWITCH_LOCK}); continuing with cached secrets only." | tee -a "$LOGFILE" >&2
fi

# --- Discord webhook ---------------------------------------------------------
# Resolved first so a run that cannot get it spends nothing else and contacts
# nothing. Kept out of the environment until ansible-playbook, so vault-pass.sh
# and anything else this script starts never see it.
webhook="${UPTIME_KUMA_DISCORD_WEBHOOK_URL:-}"
unset UPTIME_KUMA_DISCORD_WEBHOOK_URL
if [[ -n "$webhook" ]]; then
    echo "Using UPTIME_KUMA_DISCORD_WEBHOOK_URL from the environment; 1Password not consulted." | tee -a "$LOGFILE" >&2
elif ! webhook=$(cached_op_read "$WEBHOOK_SECRET_SLUG" "$WEBHOOK_SECRET_OP_PATH") || [[ -z "$webhook" ]]; then
    webhook=""
    {
        echo "ERROR: no Discord webhook: nothing usable cached in ${OP_SECRET_CACHE_DIR}/${WEBHOOK_SECRET_SLUG}"
        echo "       and 1Password could not be read (kill switch active, rate limited, or op failed)."
        echo "       Not running ${PLAYBOOK}. The host was not contacted; a running Kuma keeps"
        echo "       its current notification settings."
        echo "       To proceed without 1Password, export UPTIME_KUMA_DISCORD_WEBHOOK_URL (the #alerts"
        echo "       webhook, 1Password item vausmfy2q2m57r6scvziyrc7lq, field credential) and rerun."
    } | tee -a "$LOGFILE" >&2
    exit 1
fi

# Source ARA callback plugin environment (records runs to ARA database)
if [[ -f "$ARA_ENV_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$ARA_ENV_FILE"
fi

cd "$REPO_DIR" || exit 1

# No --diff by default: the monitor files hold the webhook and push token, and
# only their tasks' no_log keeps them out of a diff.
echo "=== Running ${PLAYBOOK} -i ${INVENTORY} ${passthrough[*]} ===" | tee -a "$LOGFILE"
tmpfile=$(mktemp)
UPTIME_KUMA_DISCORD_WEBHOOK_URL="$webhook" \
    ansible-playbook "playbooks/${PLAYBOOK}" -i "$INVENTORY" "${passthrough[@]}" 2>&1 | tee "$tmpfile"
rc=${PIPESTATUS[0]}
cat "$tmpfile" >> "$LOGFILE"
# A run that hit the rate limit trips the kill switch for everything else. The
# anchored scanner matches real op errors only, never diff or comment text (#160).
op_killswitch_scan_playbook_output "$tmpfile" || true

failed_hosts=""
if [[ $rc -ne 0 ]]; then
    failed_hosts=$(grep -E '(failed=[1-9]|unreachable=[1-9])' "$tmpfile" \
        | awk '{print $1}' \
        | sort -u \
        | paste -sd ',' -)
fi
changed_hosts=$(grep -E 'changed=[1-9]' "$tmpfile" \
    | awk '{print $1}' \
    | sort -u \
    | paste -sd ',' -)
rm -f "$tmpfile"

if [[ $rc -eq 0 ]]; then
    echo "=== ${PLAYBOOK} succeeded (changed hosts: ${changed_hosts:-none}). Log: ${LOGFILE} ===" | tee -a "$LOGFILE"
else
    echo "=== ${PLAYBOOK} FAILED rc=${rc} (failed hosts: ${failed_hosts:-unknown}). Log: ${LOGFILE} ===" | tee -a "$LOGFILE" >&2
fi

# Keep only last 50 log files
find "$LOG_DIR" -maxdepth 1 -name 'uptime-kuma-*.log' -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | tail -n +51 | cut -d' ' -f2- | xargs -r -d '\n' rm --

exit "$rc"
