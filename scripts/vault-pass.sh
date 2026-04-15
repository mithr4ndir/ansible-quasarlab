#!/usr/bin/env bash
# Pulls Ansible vault password from 1Password, via the wrapper secret
# cache so each `ansible-playbook` invocation (which spawns this script)
# does not hit 1Password every time.
#
# Falls back to .vault_pass file if op CLI is unavailable, the
# 1Password rate-limit kill switch is tripped, or the cache and op
# are both empty.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/op-killswitch.sh
source "${SCRIPT_DIR}/lib/op-killswitch.sh"
# shellcheck source=lib/op-secret-cache.sh
source "${SCRIPT_DIR}/lib/op-secret-cache.sh"

export OP_SERVICE_ACCOUNT_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-$(cat ~/.config/op/service-account-token 2>/dev/null || true)}"

fallback_vault_pass() {
    local vault_file="${SCRIPT_DIR}/../.vault_pass"
    if [[ -f "$vault_file" ]]; then
        cat "$vault_file"
        exit 0
    fi
    echo "ERROR: Cannot retrieve vault password from 1Password or .vault_pass" >&2
    exit 1
}

# cached_op_read handles the kill-switch check, cache freshness, op
# invocation, and stale-fallback internally. It returns rc=0 with the
# value on stdout, or rc=1 when nothing is available at all.
if value=$(cached_op_read ansible_vault_password "op://Infrastructure/Ansible Vault Password/password"); then
    printf '%s' "$value"
    exit 0
fi

fallback_vault_pass
