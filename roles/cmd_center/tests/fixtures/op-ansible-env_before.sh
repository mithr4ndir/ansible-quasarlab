# 1Password service account token for Ansible dynamic inventory
if [[ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
    _token_file="${HOME}/.config/op/service-account-token"
    if [[ -f "$_token_file" ]]; then
        export OP_SERVICE_ACCOUNT_TOKEN="$(cat "$_token_file")"
    fi
    unset _token_file
fi

if [[ -z "${PROXMOX_TOKEN_SECRET:-}" ]] && [[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]] && command -v op &>/dev/null; then
    export PROXMOX_TOKEN_SECRET="$(op read "op://Infrastructure/Proxmox API/Ansible Inventory/token_secret" 2>/dev/null || true)"
fi
