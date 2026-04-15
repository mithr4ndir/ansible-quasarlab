#!/usr/bin/env bash
# Pulls Ansible vault password from 1Password.
# Falls back to .vault_pass file if op CLI is unavailable OR the
# 1Password rate-limit kill-switch is tripped.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/op-killswitch.sh
source "${SCRIPT_DIR}/lib/op-killswitch.sh"

export OP_SERVICE_ACCOUNT_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-$(cat ~/.config/op/service-account-token 2>/dev/null || true)}"

# Fallback path used when op is unavailable, the killswitch is tripped,
# or the op call fails for any reason.
fallback_vault_pass() {
    local vault_file="${SCRIPT_DIR}/../.vault_pass"
    if [[ -f "$vault_file" ]]; then
        cat "$vault_file"
        exit 0
    fi
    echo "ERROR: Cannot retrieve vault password from 1Password or .vault_pass" >&2
    exit 1
}

# Short-circuit to fallback if killswitch is active; do NOT call op.
if op_killswitch_is_active; then
    fallback_vault_pass
fi

if [[ -n "$OP_SERVICE_ACCOUNT_TOKEN" ]] && command -v op &>/dev/null; then
    op_err=$(mktemp)
    if op read "op://Infrastructure/Ansible Vault Password/password" 2>"$op_err"; then
        rm -f "$op_err"
        exit 0
    fi
    # op failed; check whether it was a rate-limit and trip the switch
    op_killswitch_scan_file "$op_err" || true
    rm -f "$op_err"
fi

fallback_vault_pass
