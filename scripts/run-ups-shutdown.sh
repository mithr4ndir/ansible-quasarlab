#!/usr/bin/env bash
#
# Operator-run wrapper for playbooks/ups-shutdown.yml. Not on a timer.
#
# The playbook deploys NUT upsmon to the Proxmox hosts and needs the upsmon
# password. It used to be exported by hand from a raw `op read`, the last secret
# fetch in the repo that bypassed the cache and the kill switch. This resolves it
# through cached_op_read instead, with the guard rails run-cmd-center.sh has:
# merged code only (automation checkout pinned to origin/main), quota pre-flight,
# the Proxmox token from ansible-vault, inventory via resolve-inventory.sh, a log
# file, and a failure that is reported rather than swallowed.
#
# Scope: this configures the shutdown, it is not the shutdown. On power loss,
# upsmon on each host uses the password already in /etc/nut/upsmon.conf and
# nothing touches 1Password or this script.
#
# Usage:
#   scripts/run-ups-shutdown.sh [--limit PATTERN] [ansible-playbook args...]
#
#   The playbook targets the proxmox group; --limit narrows it. Anything else is
#   passed through to ansible-playbook, e.g. --check. -i/--inventory is refused:
#   inventory always comes from resolve-inventory.sh.
#
# Password resolution, first match wins:
#   1. NUT_MONITOR_PASSWORD already set: used as is. No op call, not cached.
#   2. cached_op_read nut_monitor_password. A fresh cache makes no op call. A
#      stale one is refreshed if 1Password answers, and served as is when op
#      fails, is rate limited, or the kill switch is active.
#   3. Nothing cached and no live read possible: exit 1 before any host is
#      contacted. Every host keeps its current upsmon.conf and still shuts down
#      on battery. Deploying without the password would replace a working config
#      with one that cannot log in to the NUT primary. To proceed with 1Password
#      unavailable, export NUT_MONITOR_PASSWORD (the upsmon user's password, also
#      set in the TrueNAS UPS service) and rerun.
#
# The kill switch does not stop this wrapper, because serving cached secrets is
# exactly what it is for. The quota pre-flight does, same as every playbook via
# callback_plugins/op_quota_gate.py. OP_QUOTA_GATE_BYPASS=1 skips both, and with
# a warm cache the run then makes no billable 1Password call.
#
# Exit status: the ansible-playbook exit code; 0 when the quota pre-flight
# skipped the run (it says so on stderr); 1 on a setup failure, including no
# password; 2 on bad arguments.
#
set -uo pipefail

# Automation checkout, NOT the operator working tree. Force-synced to
# origin/main below so a manual run applies merged code, same as the timers.
REPO_DIR="${ANSIBLE_AUTOMATION_REPO_DIR:-/var/lib/ansible-quasarlab/repo}"
LOG_DIR="${ANSIBLE_LOG_DIR:-/var/log/ansible-quasarlab}"
# Defined before anything is sourced: sync-repo output and resolve-inventory.sh
# both write to it.
LOGFILE="${LOG_DIR}/ups-shutdown-$(date +%Y%m%d-%H%M%S).log"
ARA_ENV_FILE="${ARA_ENV_FILE:-/etc/profile.d/ara-ansible-env.sh}"
PLAYBOOK="ups-shutdown.yml"
NUT_SECRET_SLUG="nut_monitor_password"
NUT_SECRET_OP_PATH="op://Infrastructure/NUT upsmon/password"

usage() {
    sed -n '/^# Usage:/,/^# Password resolution/{/^# Password resolution/d;s/^# \{0,1\}//;p;}' "$0" >&2
    exit 2
}

# --- Arguments ---------------------------------------------------------------
limit=""
passthrough=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -l|--limit)
            [[ $# -ge 2 ]] || { echo "ERROR: $1 needs a value" >&2; exit 2; }
            limit="$2"
            limit_set=1
            shift 2
            ;;
        --limit=*)
            limit="${1#--limit=}"
            limit_set=1
            shift
            ;;
        -i|--inventory|--inventory-file|-i*|--inventory=*|--inventory-file=*)
            echo "ERROR: $1 is not allowed; inventory comes from scripts/resolve-inventory.sh." >&2
            exit 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            passthrough+=("$1")
            shift
            ;;
    esac
done

# SECURITY: allowlist the host pattern. Rejects a leading `@` (read hosts from a
# file), a leading `-` (option injection), whitespace and shell metacharacters.
limit_args=()
if [[ -n "${limit_set:-}" ]]; then
    if [[ ! "$limit" =~ ^[A-Za-z0-9_.*:!\&,-]+$ || "$limit" == -* ]]; then
        echo "ERROR: invalid --limit pattern: ${limit//[^A-Za-z0-9_.*:!&,-]/?}" >&2
        exit 2
    fi
    limit_args=(--limit "$limit")
fi

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
# shellcheck source=lib/proxmox-vault.sh
source "${REPO_DIR}/scripts/lib/proxmox-vault.sh"

# Exported before the pre-flight, which reads the quota with `op service-account
# ratelimit` and fails open without a token. Reading the file costs no op call.
export OP_SERVICE_ACCOUNT_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-$(cat ~/.config/op/service-account-token 2>/dev/null || true)}"

op_preflight_check_or_exit

# Informational only; see the header for why the switch does not stop this run.
if op_killswitch_is_active; then
    echo "1Password kill switch is active (${OP_KILLSWITCH_LOCK}); continuing with cached secrets only." | tee -a "$LOGFILE" >&2
fi

# --- NUT monitor password ----------------------------------------------------
# Resolved before the vault decrypt so a run that cannot get it spends nothing
# else and contacts nothing. Kept out of the environment until ansible-playbook,
# so ansible-inventory, curl and vault-pass.sh never see it.
nut_password="${NUT_MONITOR_PASSWORD:-}"
unset NUT_MONITOR_PASSWORD
if [[ -n "$nut_password" ]]; then
    echo "Using NUT_MONITOR_PASSWORD from the environment; 1Password not consulted." | tee -a "$LOGFILE" >&2
elif ! nut_password=$(cached_op_read "$NUT_SECRET_SLUG" "$NUT_SECRET_OP_PATH") || [[ -z "$nut_password" ]]; then
    nut_password=""
    {
        echo "ERROR: no NUT monitor password: nothing usable cached in ${OP_SECRET_CACHE_DIR}/${NUT_SECRET_SLUG}"
        echo "       and 1Password could not be read (kill switch active, rate limited, or op failed)."
        echo "       Not running ${PLAYBOOK}. No host was contacted; each keeps its current"
        echo "       /etc/nut/upsmon.conf and still shuts down on battery."
        echo "       To proceed without 1Password, export NUT_MONITOR_PASSWORD (the upsmon user's"
        echo "       password from the TrueNAS UPS service) and rerun this script."
    } | tee -a "$LOGFILE" >&2
    exit 1
fi

# Decrypt the Proxmox API token from ansible-vault before resolve-inventory.sh
# and ansible-playbook, both of which need PROXMOX_TOKEN_SECRET. Issue #124.
if ! load_proxmox_token_from_vault; then
    echo "ERROR: failed to decrypt Proxmox API token from ansible-vault." | tee -a "$LOGFILE" >&2
    echo "       See docs/vault.md for recovery steps." >&2
    exit 1
fi

# Source ARA callback plugin environment (records runs to ARA database)
if [[ -f "$ARA_ENV_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$ARA_ENV_FILE"
fi

cd "$REPO_DIR" || exit 1

# Resolve inventory with fallback to cache
# shellcheck source=resolve-inventory.sh
source "${REPO_DIR}/scripts/resolve-inventory.sh"

# INVENTORY_ARGS is either empty or "-i <cache path>"; split it into argv.
read -r -a inventory_args <<< "${INVENTORY_ARGS:-}"

# No --diff by default. upsmon.conf carries the password and only its task's
# no_log keeps it out of the diff; the operator can still pass --diff.
echo "=== Running ${PLAYBOOK} ${limit_args[*]} ${passthrough[*]} ===" | tee -a "$LOGFILE"
tmpfile=$(mktemp)
NUT_MONITOR_PASSWORD="$nut_password" \
    ansible-playbook "playbooks/${PLAYBOOK}" "${inventory_args[@]}" "${limit_args[@]}" \
    "${passthrough[@]}" 2>&1 | tee "$tmpfile"
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
find "$LOG_DIR" -maxdepth 1 -name 'ups-shutdown-*.log' -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | tail -n +51 | cut -d' ' -f2- | xargs -r -d '\n' rm --

exit "$rc"
