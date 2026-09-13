#!/usr/bin/env bash
#
# Operator-run wrapper for playbooks/cmd_center.yml. Not on a timer.
#
# Gives a manual cmd_center run the same guard rails as run-proxmox.sh and
# run-security.sh: merged code only (automation checkout pinned to
# origin/main), 1Password kill switch and quota pre-flight, the Proxmox token
# from ansible-vault, inventory via resolve-inventory.sh, a log file, and a
# failure that is reported rather than swallowed.
#
# Usage:
#   scripts/run-cmd-center.sh [--limit PATTERN] [ansible-playbook args...]
#
#   --limit defaults to command-center1. Anything else is passed through to
#   ansible-playbook, e.g. --check, --tags shell_env. -i/--inventory is
#   refused: inventory always comes from resolve-inventory.sh.
#
# Exit status: the ansible-playbook exit code; 0 when the kill switch or the
# quota pre-flight skipped the run (both say so on stderr); 1 on a setup
# failure; 2 on bad arguments.
#
# Cost note: this wrapper removes no 1Password reads from the playbook itself.
# The ~88 reads a run used to cost came from ~/.bashrc on the target running
# `op read` for every SSH command; roles/cmd_center/tasks/shell_env.yml fixes that.
#
set -uo pipefail

# Automation checkout, NOT the operator working tree. Force-synced to
# origin/main below so a manual run applies merged code, same as the timers.
REPO_DIR="${ANSIBLE_AUTOMATION_REPO_DIR:-/var/lib/ansible-quasarlab/repo}"
LOG_DIR="${ANSIBLE_LOG_DIR:-/var/log/ansible-quasarlab}"
LOGFILE="${LOG_DIR}/cmd-center-$(date +%Y%m%d-%H%M%S).log"
ARA_ENV_FILE="${ARA_ENV_FILE:-/etc/profile.d/ara-ansible-env.sh}"
PLAYBOOK="cmd_center.yml"
DEFAULT_LIMIT="command-center1"

usage() {
    sed -n '/^# Usage:/,/^# Exit status/{/^# Exit status/d;s/^# \{0,1\}//;p;}' "$0" >&2
    exit 2
}

# --- Arguments ---------------------------------------------------------------
limit="$DEFAULT_LIMIT"
passthrough=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -l|--limit)
            [[ $# -ge 2 ]] || { echo "ERROR: $1 needs a value" >&2; exit 2; }
            limit="$2"
            shift 2
            ;;
        --limit=*)
            limit="${1#--limit=}"
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
# The value is passed as its own argv element either way, never through a shell.
if [[ ! "$limit" =~ ^[A-Za-z0-9_.*:!\&,-]+$ || "$limit" == -* ]]; then
    echo "ERROR: invalid --limit pattern: ${limit//[^A-Za-z0-9_.*:!&,-]/?}" >&2
    exit 2
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

# Pin the automation checkout to its remote ref. A failed sync must never fall
# through to running the playbook from an untrusted tree.
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

# Unlike run-proxmox.sh and run-security.sh, the token is exported BEFORE the
# kill switch and pre-flight. Both read the quota with `op service-account
# ratelimit`, which fails without a token, and the pre-flight fails open on an
# unreadable quota. In those two wrappers it has logged "could not read 1P
# account quota; proceeding anyway" on every systemd fire, so it never gates.
# Reading the token file costs no 1Password call.
export OP_SERVICE_ACCOUNT_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-$(cat ~/.config/op/service-account-token 2>/dev/null || true)}"

# Same gate as the timers, but say so on the terminal: a human is watching and
# a silent exit 0 reads as "ran fine".
if op_killswitch_is_active; then
    logger -t op-killswitch "kill-switch active (lock $OP_KILLSWITCH_LOCK); skipping $(basename "$0")"
    echo "1Password kill switch is active (${OP_KILLSWITCH_LOCK}); not running ${PLAYBOOK}." | tee -a "$LOGFILE" >&2
    exit 0
fi
# Proactive check: refuse to start when the account quota is already spent.
op_preflight_check_or_exit

# Decrypt the Proxmox API token from ansible-vault before resolve-inventory.sh
# and ansible-playbook, both of which need PROXMOX_TOKEN_SECRET. Issue #124.
if ! load_proxmox_token_from_vault; then
    echo "ERROR: failed to decrypt Proxmox API token from ansible-vault." | tee -a "$LOGFILE" >&2
    echo "       See docs/vault.md for recovery steps." >&2
    exit 1
fi

# No load_cached_secrets preload. The timer wrappers preload secrets their
# playbooks read with lookup('env', ...); cmd_center.yml reads none. Its only
# 1Password-backed secret is the vault password, which vault-pass.sh reads
# through cached_op_read on first use (the vault decrypt just above), so every
# later ansible process in this run is served from the cache.

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

echo "=== Running ${PLAYBOOK} --limit ${limit} ${passthrough[*]} ===" | tee -a "$LOGFILE"
tmpfile=$(mktemp)
# tee rather than the timers' plain redirect: a human is watching this run.
ansible-playbook "playbooks/${PLAYBOOK}" "${inventory_args[@]}" --limit "$limit" --diff \
    "${passthrough[@]}" 2>&1 | tee "$tmpfile"
rc=${PIPESTATUS[0]}
cat "$tmpfile" >> "$LOGFILE"
# A run that hit the rate limit trips the kill switch for everything else.
op_killswitch_scan_file "$tmpfile" || true

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
find "$LOG_DIR" -maxdepth 1 -name 'cmd-center-*.log' -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | tail -n +51 | cut -d' ' -f2- | xargs -r -d '\n' rm --

exit "$rc"
