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
#     value=$(cached_op_read wazuh_password "op://Infrastructure/Wazuh SIEM/password")
#
# Cache file layout:
#     /var/lib/ansible-quasarlab/secrets/<slug>          mode 0600, ladino-owned
#     /var/lib/ansible-quasarlab/secrets/.<slug>.lock    flock target, never deleted
# The slug is a filesystem-safe name the caller picks (letters, digits,
# `_`, `.`, `-`, not starting with `.`). Contents are the raw secret
# value with no trailing newline.
#
# TTL behavior:
# - If cache file exists and is younger than OP_SECRET_CACHE_TTL_SECS
#   (default 172800 = 48h), return it without calling op.
# - If stale or missing, take a per-slug flock, then re-check. If another
#   process refreshed the slug while we waited, serve that value. So
#   concurrent misses on one slug collapse into a single op call.
# - Otherwise call op. On success, update the cache and return the new
#   value. On op failure (rate limited, etc.):
#     * if a stale cache exists, return it with a syslog warning.
#     * if no cache exists at all, return empty string with rc=1 so
#       the caller can decide how to handle the missing secret.
# - The kill switch is checked before op. If tripped, we do not call op
#   at all (behaves like "stale cache, op unavailable").
#
# Locking: at most one slug lock is held at a time and it is released
# before cached_op_read returns, so a process reading several slugs in a
# row cannot deadlock against itself. The lock fd is closed for the op
# child so a lingering op helper process can never pin the lock.
#
# Forcing a refresh (after changing an item in 1Password):
#     scripts/op-secret-refresh.sh <slug> [<slug>...]
#     scripts/op-secret-refresh.sh --all
# This marks the cached value stale without deleting it. The next reader
# re-reads it once; if that read fails the old value is still served.

OP_SECRET_CACHE_DIR="${OP_SECRET_CACHE_DIR:-/var/lib/ansible-quasarlab/secrets}"
# 48h. Secrets here change rarely and any change is followed by a manual
# op-secret-refresh.sh, so a long TTL costs nothing in correctness.
OP_SECRET_CACHE_TTL_SECS="${OP_SECRET_CACHE_TTL_SECS:-172800}"
# How long a reader waits for another process refreshing the same slug.
# On timeout it serves stale if it has it, otherwise reads unlocked.
OP_SECRET_CACHE_LOCK_TIMEOUT_SECS="${OP_SECRET_CACHE_LOCK_TIMEOUT_SECS:-90}"

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

# SECURITY: slugs become file names, so allowlist them. Rejects `/`, `..`,
# leading dots (which would collide with lock and temp files) and control
# characters.
op_secret_cache_valid_slug() {
    [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]
}

# Returns 0 if the cache file exists and is younger than the TTL.
op_secret_cache_is_fresh() {
    local cache_file="$1" mtime
    [[ -f "$cache_file" ]] || return 1
    mtime=$(stat -c %Y "$cache_file" 2>/dev/null) || return 1
    (( $(date +%s) - mtime < OP_SECRET_CACHE_TTL_SECS ))
}

# cached_op_read <slug> <op_path>
# Echoes the secret value on stdout. Returns 0 on success, 1 if neither
# a fresh nor stale cached value nor a live op read could produce one.
cached_op_read() {
    local slug="$1"
    local op_path="$2"
    local cache_file lock_fd="" rc

    if ! op_secret_cache_valid_slug "$slug"; then
        logger -t op-secret-cache "rejected invalid slug=${slug//[^A-Za-z0-9_.-]/?}"
        return 1
    fi
    cache_file="${OP_SECRET_CACHE_DIR}/${slug}"

    op_secret_cache_init

    # Fresh cache: return immediately, no op call, no lock.
    if op_secret_cache_is_fresh "$cache_file"; then
        cat "$cache_file"
        return 0
    fi

    # Stale or missing. Serialize the refresh per slug. The group redirect
    # keeps a failed open quiet without redirecting the caller's stderr.
    if command -v flock >/dev/null 2>&1 \
        && { exec {lock_fd}>>"${OP_SECRET_CACHE_DIR}/.${slug}.lock"; } 2>/dev/null; then
        if ! flock -w "$OP_SECRET_CACHE_LOCK_TIMEOUT_SECS" "$lock_fd"; then
            exec {lock_fd}>&-
            lock_fd=""
            if [[ -f "$cache_file" ]]; then
                logger -t op-secret-cache "lock wait timed out; serving stale cache for slug=${slug}"
                cat "$cache_file"
                return 0
            fi
            logger -t op-secret-cache "lock wait timed out and no cache for slug=${slug}; reading unlocked"
        fi
    else
        lock_fd=""
        logger -t op-secret-cache "could not lock slug=${slug}; reading unlocked"
    fi

    _op_secret_cache_refresh "$slug" "$op_path" "$cache_file" "$lock_fd"
    rc=$?
    [[ -n "$lock_fd" ]] && exec {lock_fd}>&-
    return "$rc"
}

# Body of cached_op_read once the slug lock is held (or could not be).
# Arguments: <slug> <op_path> <cache_file> <lock_fd or empty>
_op_secret_cache_refresh() {
    local slug="$1" op_path="$2" cache_file="$3" lock_fd="$4"

    # Someone else refreshed this slug while we waited on the lock.
    if op_secret_cache_is_fresh "$cache_file"; then
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

    # OP_SHIM_SLUG tags the read for the op attribution shim (see
    # roles/op_ratelimit_collector/files/op-shim). The real op ignores it.
    local op_err value tmp_file
    op_err=$(mktemp)
    if [[ -n "$lock_fd" ]]; then
        value=$(OP_SHIM_SLUG="$slug" op read "$op_path" 2>"$op_err" {lock_fd}>&- || true)
    else
        value=$(OP_SHIM_SLUG="$slug" op read "$op_path" 2>"$op_err" || true)
    fi
    if [[ -n "$value" ]]; then
        # Write atomically with 0600 perms. mktemp gives a unique name so
        # an unlocked fallback reader cannot interleave with a locked one.
        if tmp_file=$(umask 0077 && mktemp "${OP_SECRET_CACHE_DIR}/.${slug}.XXXXXX" 2>/dev/null); then
            if printf '%s' "$value" > "$tmp_file" && mv -f "$tmp_file" "$cache_file" 2>/dev/null; then
                chmod 0600 "$cache_file" 2>/dev/null || true
            else
                rm -f "$tmp_file"
                logger -t op-secret-cache "cache write failed for slug=${slug}"
            fi
        else
            logger -t op-secret-cache "cache write failed for slug=${slug}"
        fi
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

# op_secret_cache_invalidate <slug> [<slug>...]
# Force the next read of each slug to go to 1Password. Sets the cache
# file mtime to the epoch instead of deleting it, so a failed refresh
# still has the old value to fall back on. Makes no op call. Takes the
# slug lock briefly so it cannot race a refresh that is mid-write.
# Returns 1 if any slug was invalid or could not be invalidated.
op_secret_cache_invalidate() {
    local slug cache_file lock_fd rc=0
    for slug in "$@"; do
        if ! op_secret_cache_valid_slug "$slug"; then
            echo "op-secret-cache: invalid slug: ${slug//[^A-Za-z0-9_.-]/?}" >&2
            rc=1
            continue
        fi
        cache_file="${OP_SECRET_CACHE_DIR}/${slug}"
        if [[ ! -f "$cache_file" ]]; then
            echo "op-secret-cache: ${slug} is not cached; the next read fetches it anyway" >&2
            continue
        fi
        lock_fd=""
        if command -v flock >/dev/null 2>&1 \
            && { exec {lock_fd}>>"${OP_SECRET_CACHE_DIR}/.${slug}.lock"; } 2>/dev/null; then
            flock -w 10 "$lock_fd" || true
        else
            lock_fd=""
        fi
        if touch -d @0 "$cache_file" 2>/dev/null; then
            logger -t op-secret-cache "invalidated slug=${slug}"
        else
            echo "op-secret-cache: could not invalidate ${slug}" >&2
            rc=1
        fi
        [[ -n "$lock_fd" ]] && exec {lock_fd}>&-
    done
    return "$rc"
}

# Echo every cached slug, one per line. Skips lock and temp files.
op_secret_cache_list_slugs() {
    local f slug
    for f in "$OP_SECRET_CACHE_DIR"/*; do
        [[ -f "$f" ]] || continue
        slug="${f##*/}"
        op_secret_cache_valid_slug "$slug" && printf '%s\n' "$slug"
    done
    return 0
}

# Pre-populate the env with a set of playbook secrets using the cache.
# Called once at the top of each wrapper after the killswitch check.
# Arguments: pairs of "<ENV_VAR> <slug> <op_path>" lines on stdin OR via
# a here-doc. Using function arguments would be clearer but bash is
# awkward about arrays with spaces in paths.
#
# Example:
#     load_cached_secrets <<'EOF'
#     WAZUH_PASSWORD         wazuh_password          op://Infrastructure/Wazuh SIEM/password
#     AUTHENTIK_PG_PASSWORD  authentik_pg_password   op://Infrastructure/Authentik/PostgreSQL Password
#     EOF
# Note: the Proxmox API token used to live here. It is now in
# ansible-vault and loaded via scripts/lib/proxmox-vault.sh, since
# the dynamic inventory plugin loads in subprocess scope outside the
# env-cache path (see issue #124).
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
