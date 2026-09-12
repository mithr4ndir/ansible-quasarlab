#!/usr/bin/env bash
# Force a refresh of cached 1Password secrets after changing an item.
#
# Marks cache entries stale (see op_secret_cache_invalidate in
# lib/op-secret-cache.sh). It never calls op itself: the next scheduled
# run, or anything else that calls cached_op_read, re-reads each stale
# slug exactly once. If that read fails the previous value is served.
#
# Usage:
#     scripts/op-secret-refresh.sh --list              show cached slugs and their age
#     scripts/op-secret-refresh.sh <slug> [<slug>...]  refresh these slugs
#     scripts/op-secret-refresh.sh --all               refresh every cached slug
#
# To have the new value picked up now rather than at the next timer fire:
#     sudo systemctl start ansible-proxmox.service   (or ansible-security.service)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/op-secret-cache.sh
source "${SCRIPT_DIR}/lib/op-secret-cache.sh"

usage() {
    sed -n 's/^#     //p' "$0" >&2
    exit 2
}

[[ $# -ge 1 ]] || usage

case "$1" in
    --list)
        now=$(date +%s)
        printf '%-40s %12s  %s\n' SLUG AGE_SECS STATE
        while IFS= read -r slug; do
            mtime=$(stat -c %Y "${OP_SECRET_CACHE_DIR}/${slug}")
            age=$(( now - mtime ))
            state=fresh
            (( age < OP_SECRET_CACHE_TTL_SECS )) || state=stale
            printf '%-40s %12s  %s\n' "$slug" "$age" "$state"
        done < <(op_secret_cache_list_slugs)
        ;;
    --all)
        mapfile -t slugs < <(op_secret_cache_list_slugs)
        if [[ ${#slugs[@]} -eq 0 ]]; then
            echo "No cached slugs in ${OP_SECRET_CACHE_DIR}." >&2
            exit 0
        fi
        op_secret_cache_invalidate "${slugs[@]}"
        echo "Marked ${#slugs[@]} slug(s) stale; each is re-read once on next use."
        ;;
    -h|--help|-*)
        usage
        ;;
    *)
        op_secret_cache_invalidate "$@"
        echo "Marked $# slug(s) stale; each is re-read once on next use."
        ;;
esac
