#!/usr/bin/env bash
# File-backed cache for 1Password secret reads.
#
# Why: scheduled ansible runs, vault-pass, and per-playbook tasks each
# fire `op read` calls whose values do not change between runs. Caching
# them on disk with a TTL cuts the baseline `op read` rate from tens
# per hour to a handful per day. Works with the kill switch (see
# op-killswitch.sh): when the switch is tripped, cached values are
# still served; we just never call `op` to refresh them.
#
# Usage (sourced):
#     source "${SCRIPT_DIR}/lib/op-secret-cache.sh"
#     value=$(cached_op_read proxmox_token "op://Infrastructure/Proxmox API/Ansible Inventory/token_secret")
#
# Cache file layout:
#     /var/lib/ansible-quasarlab/secrets/<slug>     mode 0600, ladino-owned
# The slug is any filesystem-safe name the caller picks. Contents are
# the raw secret value with no trailing newline.
#
# TTL behavior:
# - If cache file exists and is younger than OP_SECRET_CACHE_TTL_SECS
#   (default 43200 = 12h), return it without calling op.
# - If stale or missing, call op. On success, update the cache and
#   return the new value. On op failure (rate limited, etc.):
#     * if a stale cache exists, return it with a syslog warning.
#     * if no cache exists at all, return empty string with rc=1 so
#       the caller can decide how to handle the missing secret.
# - The kill switch is checked first. If tripped, we do not call op
#   at all (behaves like "stale cache, op unavailable").

OP_SECRET_CACHE_DIR="${OP_SECRET_CACHE_DIR:-/var/lib/ansible-quasarlab/secrets}"
OP_SECRET_CACHE_TTL_SECS="${OP_SECRET_CACHE_TTL_SECS:-43200}"

# Depend on the kill-switch library already being sourced by the caller.
# If it is not, define a stub so this library still works standalone.
if ! declare -F op_killswitch_is_active >/dev/null; then
    op_killswitch_is_active() { return 1; }
    op_killswitch_scan_file() { return 1; }
fi

op_secret_cache_init() {
    if [[ ! -d "$OP_SECRET_CACHE_DIR" ]]; then
        mkdir -p "$OP_SECRET_CACHE_DIR" 2>/dev/null || true
        chmod 0700 "$OP_SECRET_CACHE_DIR" 2>/dev/null || true
    fi
}

# cached_op_read <slug> <op_path>
# Echoes the secret value on stdout. Returns 0 on success, 1 if neither
# a fresh nor stale cached value nor a live op read could produce one.
cached_op_read() {
    local slug="$1"
    local op_path="$2"
    local cache_file="${OP_SECRET_CACHE_DIR}/${slug}"
    local now cache_age cache_fresh=0
    now=$(date +%s)

    op_secret_cache_init

    if [[ -f "$cache_file" ]]; then
        cache_age=$(( now - $(stat -c %Y "$cache_file" 2>/dev/null || echo 0) ))
        if (( cache_age < OP_SECRET_CACHE_TTL_SECS )); then
            cache_fresh=1
        fi
    fi

    # Fresh cache: return immediately, no op call.
    if (( cache_fresh )); then
        cat "$cache_file"
        return 0
    fi

    # Kill switch active: do not call op. Serve stale if we have it.
    if op_killswitch_is_active; then
        if [[ -f "$cache_file" ]]; then
            logger -t op-secret-cache "killswitch active; serving stale cache for slug=${slug}"
            cat "$cache_file"
            return 0
        fi
        logger -t op-secret-cache "killswitch active AND no cache for slug=${slug}; returning empty"
        return 1
    fi

    # Need a live read. Require op present and token set.
    if ! command -v op >/dev/null 2>&1 || [[ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
        if [[ -f "$cache_file" ]]; then
            logger -t op-secret-cache "op unavailable; serving stale cache for slug=${slug}"
            cat "$cache_file"
            return 0
        fi
        return 1
    fi

    local op_err value
    op_err=$(mktemp)
    value=$(op read "$op_path" 2>"$op_err" || true)
    if [[ -n "$value" ]]; then
        # Write atomically with 0600 perms.
        (
            umask 0077
            printf '%s' "$value" > "${cache_file}.tmp"
        )
        mv "${cache_file}.tmp" "$cache_file" 2>/dev/null || true
        chmod 0600 "$cache_file" 2>/dev/null || true
        rm -f "$op_err"
        printf '%s' "$value"
        return 0
    fi

    # op call failed. Scan for rate limit and trip the switch if so.
    op_killswitch_scan_file "$op_err" || true
    rm -f "$op_err"

    if [[ -f "$cache_file" ]]; then
        logger -t op-secret-cache "op failed; serving stale cache for slug=${slug}"
        cat "$cache_file"
        return 0
    fi
    return 1
}

# Pre-populate the env with a set of playbook secrets using the cache.
# Called once at the top of each wrapper after the killswitch check.
# Arguments: pairs of "<ENV_VAR> <slug> <op_path>" lines on stdin OR via
# a here-doc. Using function arguments would be clearer but bash is
# awkward about arrays with spaces in paths.
#
# Example:
#     load_cached_secrets <<'EOF'
#     PROXMOX_TOKEN_SECRET   proxmox_token           op://Infrastructure/Proxmox API/Ansible Inventory/token_secret
#     AUTHENTIK_PG_PASSWORD  authentik_pg_password   op://Infrastructure/Authentik/PostgreSQL Password
#     EOF
load_cached_secrets() {
    local env_name slug op_path value
    while read -r env_name slug op_path; do
        [[ -z "$env_name" || "$env_name" == \#* ]] && continue
        if value=$(cached_op_read "$slug" "$op_path"); then
            export "${env_name}=${value}"
        else
            logger -t op-secret-cache "load_cached_secrets: no value for ${env_name} (slug=${slug})"
        fi
    done
}
