#!/usr/bin/env bash
# Pulls Ansible vault password from 1Password.
# Falls back to .vault_pass file if op CLI is unavailable.
set -euo pipefail

export OP_SERVICE_ACCOUNT_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-$(cat ~/.config/op/service-account-token 2>/dev/null || true)}"

if [[ -n "$OP_SERVICE_ACCOUNT_TOKEN" ]] && command -v op &>/dev/null; then
    op read "op://Infrastructure/Ansible Vault Password/password" 2>/dev/null && exit 0
fi

# Fallback to local file
if [[ -f "$(dirname "$0")/../.vault_pass" ]]; then
    cat "$(dirname "$0")/../.vault_pass"
else
    echo "ERROR: Cannot retrieve vault password from 1Password or .vault_pass" >&2
    exit 1
fi
