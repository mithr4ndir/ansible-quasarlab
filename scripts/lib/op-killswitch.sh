#!/usr/bin/env bash
# 1Password rate-limit kill switch.
#
# When the service account rate limit is hit, the rolling-hour window can
# stay pinned for many hours if automation keeps retrying. This library
# provides a shared lock-file based kill switch so the first script that
# detects "Too many requests" sets a lock, and every subsequent script
# run (wrappers, vault-pass, inventory resolve) exits early instead of
# making another `op` call that extends the window.
#
# Usage (sourced from another script):
#     source "${REPO_DIR}/scripts/lib/op-killswitch.sh"
#     op_killswitch_check_or_exit   # exits 0 early if lock active
#     ...
#     # after running `op` or an ansible-playbook that uses `op`:
#     op_killswitch_scan_file "$tmpfile"   # sets lock if rate-limit in output
#
# The lock file stores the Unix timestamp it was created. Treat a lock
# as active while mtime is within OP_KILLSWITCH_TTL_SECS of now (default
# 86400 = 24h). Remove manually with `rm "$OP_KILLSWITCH_LOCK"` once the
# 1P window has cleared, or wait for the TTL to expire.
#
# Prometheus metrics (via node_exporter textfile collector) are written
# on every check so dashboards and alerts can show the state.

OP_KILLSWITCH_STATE_DIR="${OP_KILLSWITCH_STATE_DIR:-/var/lib/ansible-quasarlab}"
OP_KILLSWITCH_LOCK="${OP_KILLSWITCH_LOCK:-${OP_KILLSWITCH_STATE_DIR}/1p-killswitch}"
OP_KILLSWITCH_TTL_SECS="${OP_KILLSWITCH_TTL_SECS:-86400}"
OP_KILLSWITCH_METRIC_FILE="${OP_KILLSWITCH_METRIC_FILE:-/var/lib/node_exporter/textfiles/onepassword_killswitch.prom}"

# Ensure the state dir exists with sane perms. Best-effort; failures do
# not abort the caller because permission issues should be surfaced
# explicitly, not swallowed by killswitch plumbing.
op_killswitch_init() {
    mkdir -p "$OP_KILLSWITCH_STATE_DIR" 2>/dev/null || true
}

# Returns 0 if the killswitch is currently active, 1 otherwise.
# Side effect: writes the Prometheus metric file.
op_killswitch_is_active() {
    op_killswitch_init
    local active=0
    local tripped_at=0
    local age=0

    if [[ -f "$OP_KILLSWITCH_LOCK" ]]; then
        tripped_at=$(stat -c %Y "$OP_KILLSWITCH_LOCK" 2>/dev/null || echo 0)
        age=$(( $(date +%s) - tripped_at ))
        if (( age < OP_KILLSWITCH_TTL_SECS )); then
            active=1
        fi
    fi

    op_killswitch_write_metric "$active" "$tripped_at"
    [[ $active -eq 1 ]]
}

# Writes the Prometheus textfile. Called by is_active and trip.
op_killswitch_write_metric() {
    local active="${1:-0}"
    local tripped_at="${2:-0}"
    local metric_dir
    metric_dir=$(dirname "$OP_KILLSWITCH_METRIC_FILE")
    # Best-effort: if the textfile dir is not writable, skip the metric.
    [[ -d "$metric_dir" && -w "$metric_dir" ]] || return 0

    cat > "${OP_KILLSWITCH_METRIC_FILE}.tmp" <<METRICS
# HELP onepassword_killswitch_active 1 if the 1Password rate-limit killswitch is currently tripped, 0 otherwise.
# TYPE onepassword_killswitch_active gauge
onepassword_killswitch_active ${active}
# HELP onepassword_killswitch_tripped_timestamp_seconds Unix timestamp when the current killswitch was tripped (0 if inactive).
# TYPE onepassword_killswitch_tripped_timestamp_seconds gauge
onepassword_killswitch_tripped_timestamp_seconds ${tripped_at}
# HELP onepassword_killswitch_ttl_seconds Configured kill-switch TTL in seconds; lock is auto-cleared after this.
# TYPE onepassword_killswitch_ttl_seconds gauge
onepassword_killswitch_ttl_seconds ${OP_KILLSWITCH_TTL_SECS}
METRICS
    mv "${OP_KILLSWITCH_METRIC_FILE}.tmp" "$OP_KILLSWITCH_METRIC_FILE" 2>/dev/null || true
    chmod 644 "$OP_KILLSWITCH_METRIC_FILE" 2>/dev/null || true
}

# Trip the killswitch. If an active lock already exists (mtime within
# TTL) we preserve its mtime so we do not extend the TTL on a flurry
# of errors. If the lock is stale (mtime older than TTL) we overwrite
# it so a new rate-limit event after the previous window expired gets
# a fresh TTL, instead of leaving a stale one-shot lock that would
# make op_killswitch_is_active report "inactive" and swallow the event.
op_killswitch_trip() {
    local reason="${1:-rate_limited}"
    op_killswitch_init
    local refresh=1
    if [[ -f "$OP_KILLSWITCH_LOCK" ]]; then
        local existing_mtime age
        existing_mtime=$(stat -c %Y "$OP_KILLSWITCH_LOCK" 2>/dev/null || echo 0)
        age=$(( $(date +%s) - existing_mtime ))
        if (( age < OP_KILLSWITCH_TTL_SECS )); then
            # Lock is still within TTL; leave it alone, do not log again.
            refresh=0
        fi
    fi
    if (( refresh )); then
        printf '%s trip_reason=%s\n' "$(date -u +%FT%TZ)" "$reason" > "$OP_KILLSWITCH_LOCK" 2>/dev/null || true
        chmod 644 "$OP_KILLSWITCH_LOCK" 2>/dev/null || true
        logger -t op-killswitch "1Password kill-switch TRIPPED: reason=${reason}. Further op calls are suppressed until TTL (${OP_KILLSWITCH_TTL_SECS}s) expires or the lock is removed."
    fi
    op_killswitch_write_metric 1 "$(stat -c %Y "$OP_KILLSWITCH_LOCK" 2>/dev/null || date +%s)"
}

# Scan a tmpfile from a recent op or ansible-playbook run for rate-limit
# markers. If found, trip the killswitch.
op_killswitch_scan_file() {
    local file="$1"
    [[ -f "$file" ]] || return 0
    if grep -qiE 'Too many requests|rate[- ]limited|429 Too Many' "$file" 2>/dev/null; then
        op_killswitch_trip "rate_limited"
        return 0
    fi
    return 1
}

# Call this at the very top of any wrapper / helper that is about to
# invoke `op`. If the killswitch is tripped, exit 0 silently (well, with
# a syslog line) so automation does not compound the rate-limit window.
op_killswitch_check_or_exit() {
    if op_killswitch_is_active; then
        logger -t op-killswitch "kill-switch active (lock $OP_KILLSWITCH_LOCK); skipping $(basename "${0:-unknown}")"
        exit 0
    fi
}
