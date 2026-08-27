#!/usr/bin/env bash
#
# Manual wrapper for playbooks/ups-shutdown.yml.
#
# The playbook previously expected NUT_MONITOR_PASSWORD to be exported by hand
# via a raw `op read` (see git history), the only secret fetch in the repo that
# bypassed the shared cache and killswitch. Low frequency alone did not make it
# safe: an unwrapped `op read` re-run during an actual outage, or fired more
# than once while troubleshooting, still counts against the same 24h account
# window as everything else. This routes it through cached_op_read like every
# other secret, and resolves inventory the same way run-security.sh does.
set -euo pipefail

REPO_DIR="${ANSIBLE_AUTOMATION_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO_DIR"

# shellcheck source=lib/op-killswitch.sh
source "${REPO_DIR}/scripts/lib/op-killswitch.sh"
# shellcheck source=lib/op-secret-cache.sh
source "${REPO_DIR}/scripts/lib/op-secret-cache.sh"
# shellcheck source=lib/proxmox-vault.sh
source "${REPO_DIR}/scripts/lib/proxmox-vault.sh"

export OP_SERVICE_ACCOUNT_TOKEN="${OP_SERVICE_ACCOUNT_TOKEN:-$(cat ~/.config/op/service-account-token 2>/dev/null || true)}"

if ! load_proxmox_token_from_vault; then
    echo "ERROR: failed to decrypt Proxmox API token from ansible-vault." >&2
    exit 1
fi

if ! NUT_MONITOR_PASSWORD=$(cached_op_read nut_monitor_password "op://Infrastructure/NUT upsmon/password"); then
    echo "ERROR: could not resolve the NUT monitor password from cache or 1Password." >&2
    echo "       Set NUT_MONITOR_PASSWORD yourself and run ansible-playbook directly instead." >&2
    exit 1
fi
export NUT_MONITOR_PASSWORD

# shellcheck source=resolve-inventory.sh
source "${REPO_DIR}/scripts/resolve-inventory.sh"

# shellcheck disable=SC2086 # INVENTORY_ARGS is intentionally word-split
exec ansible-playbook playbooks/ups-shutdown.yml $INVENTORY_ARGS "$@"
