#!/usr/bin/env bash
# Decrypts the Proxmox API token from ansible-vault and exports it as
# PROXMOX_TOKEN_SECRET in the current shell. Replaces the previous
# `op read` / cached_op_read path that burned 1Password rate-limit
# budget per ansible fork (issue #124, 2026-04-19 dynamic-inventory
# bypass postmortem).
#
# Why a separate lib: the dynamic Proxmox inventory plugin loads in
# subprocess scope BEFORE the wrapper's runtime env has been finalized
# and BEFORE most playbook variables are bound. Anything that needs
# the token in env (the inventory plugin, the API reachability check
# in resolve-inventory.sh, the Prometheus target sync) must source
# this lib AND call load_proxmox_token_from_vault BEFORE invoking
# ansible-playbook or ansible-inventory.
#
# Usage (sourced):
#     source "${REPO_DIR}/scripts/lib/proxmox-vault.sh"
#     if ! load_proxmox_token_from_vault; then
#         echo "ERROR: failed to decrypt Proxmox token from vault" >&2
#         exit 1
#     fi
#
# Sets: PROXMOX_TOKEN_SECRET (exported)
# Returns: 0 on success, 1 on any failure (vault file missing,
#          vault-pass script missing, decryption failed, var absent
#          from the decrypted YAML, ansible-vault binary missing).
#
# Failure mode: returns 1 silently to stderr-quiet callers (the only
# log line is via `logger` so script stdout stays clean for piping).
# Callers MUST check the return value and exit non-zero if needed,
# rather than continuing with an empty PROXMOX_TOKEN_SECRET. This is
# explicit per the spec NFR (no silent fallback to op).

_PROXMOX_VAULT_LOG_TAG="proxmox-vault"

_proxmox_vault_repo_dir() {
    if [[ -n "${REPO_DIR:-}" ]]; then
        printf '%s' "$REPO_DIR"
        return 0
    fi
    cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd
}

_proxmox_vault_extract() {
    local repo_dir vault_file vault_pass
    repo_dir="$(_proxmox_vault_repo_dir)"
    vault_file="${repo_dir}/group_vars/all/vault.yml"
    vault_pass="${repo_dir}/scripts/vault-pass.sh"

    if [[ ! -f "$vault_file" ]]; then
        logger -t "$_PROXMOX_VAULT_LOG_TAG" "vault file missing: $vault_file"
        return 1
    fi
    if [[ ! -x "$vault_pass" ]]; then
        logger -t "$_PROXMOX_VAULT_LOG_TAG" "vault-pass script not executable: $vault_pass"
        return 1
    fi
    if ! command -v ansible-vault >/dev/null 2>&1; then
        logger -t "$_PROXMOX_VAULT_LOG_TAG" "ansible-vault binary not on PATH"
        return 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        logger -t "$_PROXMOX_VAULT_LOG_TAG" "python3 not on PATH"
        return 1
    fi

    local view_err
    view_err="$(mktemp)"
    local decrypted
    if ! decrypted=$(ansible-vault view "$vault_file" --vault-password-file "$vault_pass" 2>"$view_err"); then
        logger -t "$_PROXMOX_VAULT_LOG_TAG" "ansible-vault view failed for $vault_file"
        cat "$view_err" | logger -t "$_PROXMOX_VAULT_LOG_TAG" || true
        rm -f "$view_err"
        return 1
    fi
    rm -f "$view_err"

    # Parse with python rather than grep/awk to handle quoting/whitespace
    # correctly. PyYAML is a stdlib of the ansible install, so always present.
    printf '%s' "$decrypted" | python3 -c '
import sys, yaml
try:
    data = yaml.safe_load(sys.stdin) or {}
    val = data.get("vault_proxmox_api_token", "")
    if not isinstance(val, str) or not val:
        sys.exit(1)
    sys.stdout.write(val)
except yaml.YAMLError:
    sys.exit(1)
'
}

load_proxmox_token_from_vault() {
    local val
    if ! val=$(_proxmox_vault_extract); then
        return 1
    fi
    if [[ -z "$val" ]]; then
        return 1
    fi
    export PROXMOX_TOKEN_SECRET="$val"
    return 0
}
