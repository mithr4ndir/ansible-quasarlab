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
# Headroom required before the lock auto-clears. Set well above the
# pre-flight threshold (50) so releasing the lock does not immediately hand
# the next run a quota that re-trips it.
OP_KILLSWITCH_RECOVER_MIN_REMAINING="${OP_KILLSWITCH_RECOVER_MIN_REMAINING:-200}"

# Ensure the state dir exists with sane perms. Best-effort; failures do
# not abort the caller because permission issues should be surfaced
# explicitly, not swallowed by killswitch plumbing.
op_killswitch_init() {
    mkdir -p "$OP_KILLSWITCH_STATE_DIR" 2>/dev/null || true
}

# Returns 0 when the 1Password account quota has real headroom again.
# Deliberately strict: anything unreadable returns 1 (not recovered) so an
# op outage can never be mistaken for recovery and release the lock early.
# Requires more than a bare pass of the pre-flight threshold, so a lock is
# not released straight into a window that would immediately re-trip it.
op_killswitch_quota_recovered() {
    local remaining
    remaining=$(op_preflight_remaining)
    [[ -n "$remaining" ]] || return 1
    (( remaining > OP_KILLSWITCH_RECOVER_MIN_REMAINING ))
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

    # A fixed 24h TTL is a proxy for the real condition, which is "the quota
    # came back". Those are not the same: the account window can reset hours
    # before the TTL expires, and until someone remembers to remove the lock
    # by hand the whole lab stays frozen with the staleness alerts firing.
    # That manual step has been missed before.
    #
    # Now that the quota can be read for free, check it directly and release
    # the lock once there is real headroom. Only ever clears on a positively
    # read healthy value; an unreadable quota leaves the lock exactly as it is.
    if (( active == 1 )) && op_killswitch_quota_recovered; then
        rm -f "$OP_KILLSWITCH_LOCK" 2>/dev/null || true
        logger -t op-killswitch "1Password quota recovered; kill-switch lock auto-cleared."
        active=0
        tripped_at=0
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

# Scan output captured from an op process, and nothing else, for rate-limit
# markers. If found, trip the killswitch. Deliberately broad: every byte of the
# input came from op, so any mention is op talking about its own rate limit.
# For an ansible-playbook log use op_killswitch_scan_playbook_output instead.
op_killswitch_scan_file() {
    local file="$1"
    [[ -f "$file" ]] || return 0
    if grep -qiE 'Too many requests|rate[- ]limited|429 Too Many' "$file" 2>/dev/null; then
        op_killswitch_trip "rate_limited"
        return 0
    fi
    return 1
}

# The op CLI rate-limit error line, as op writes it to stderr:
#   [ERROR] <YYYY/MM/DD> <HH:MM:SS> (429) Too Many Requests: You've reached ...
#   [ERROR] <YYYY/MM/DD> <HH:MM:SS> Too many requests. Your client has been ...
# The first form is the documented hourly and daily limit error
# (https://www.1password.dev/service-accounts/rate-limits), the second is
# embedded in the op 2.39.0 binary, and the level plus timestamp prefix is what
# op 2.39.0 prints for every error. A dash or T separated timestamp is also
# accepted in case a later op changes the layout.
OP_KILLSWITCH_OP_RATELIMIT_ERE='\[ERROR\] [0-9]{4}[/-][0-9]{2}[/-][0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}[^ ]* (\(429\) )?Too many requests'

# Scan a whole ansible-playbook log for an op rate-limit error. If found, trip
# the killswitch. Returns 0 when tripped, 1 otherwise.
#
# Why not op_killswitch_scan_file: playbook logs carry arbitrary text. On
# 2026-09-13 the --diff of this very file put "Too many requests" in the log
# and paused all automation while 1Password was refusing nothing (issue #160).
# So a match needs the op error line format above, and lines starting with
# `+` or `-` (added or removed lines of a --diff hunk, and its ---/+++ header)
# are never considered. The error is not anchored to the start of the line
# because Ansible reports a failed command's stderr inside its result, e.g.
# `fatal: [host]: FAILED! => {... "stderr": "[ERROR] ..."}`.
#
# Not caught here: op calls under no_log (Ansible replaces their stderr with a
# "censored" notice) and op calls whose stderr never reaches the log. Callers
# that run op themselves scan its stderr with op_killswitch_scan_file.
op_killswitch_scan_playbook_output() {
    local file="$1"
    [[ -f "$file" ]] || return 1
    # LC_ALL=C and -a: `.` must match any byte, so invalid UTF-8 or a NUL
    # earlier on the line cannot hide a real error.
    if LC_ALL=C grep -qaiE "^([^+-].*)?${OP_KILLSWITCH_OP_RATELIMIT_ERE}" "$file" 2>/dev/null; then
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

# ---------------------------------------------------------------------------
# Proactive pre-flight quota check.
#
# The kill switch above is reactive: it only trips once 1Password has already
# answered "Too many requests". By then the 24h window is pinned. This check
# runs BEFORE any op call and refuses to start when the account quota is
# already spent.
#
# `op service-account ratelimit` does not itself count against the quota
# (verified 2026-04-19), so this is free to call on every run.
#
# This duplicates callback_plugins/op_quota_gate.py on purpose. The callback
# covers interactive `ansible-playbook` runs but only takes effect once the
# ara role has re-templated /etc/profile.d/ara-ansible-env.sh, because
# ANSIBLE_CALLBACK_PLUGINS overrides ansible.cfg. This shell guard needs no
# deploy step, so scheduled timer runs are protected immediately.
# ---------------------------------------------------------------------------

OP_PREFLIGHT_MIN_REMAINING="${OP_PREFLIGHT_MIN_REMAINING:-50}"

# Echo the account read_write REMAINING value, or nothing if undeterminable.
op_preflight_remaining() {
    command -v op >/dev/null 2>&1 || return 0
    local out
    out=$(timeout 20 op service-account ratelimit 2>/dev/null) || return 0
    # Match the account/read_write row and sanity check that used+remaining
    # equals limit, so a future column reorder fails closed to "unknown"
    # rather than silently gating on the wrong number.
    awk '
        tolower($1) == "account" && tolower($2) == "read_write" {
            limit = $3 + 0; used = $4 + 0; remaining = $5 + 0
            if (used + remaining == limit) { print remaining }
            exit
        }
    ' <<< "$out"
}

# Exit non-zero (and log) when the quota is at or below the threshold.
# Fails OPEN: an unreadable quota lets the run proceed with a warning, because
# blocking all automation is worse than the overspend this prevents.
op_preflight_check_or_exit() {
    if [[ "${OP_QUOTA_GATE_BYPASS:-}" == "1" ]]; then
        logger -t op-preflight "pre-flight quota gate BYPASSED via OP_QUOTA_GATE_BYPASS=1"
        return 0
    fi

    local remaining
    remaining=$(op_preflight_remaining)

    if [[ -z "$remaining" ]]; then
        logger -t op-preflight "could not read 1P account quota; proceeding anyway"
        return 0
    fi

    if (( remaining <= OP_PREFLIGHT_MIN_REMAINING )); then
        logger -t op-preflight "1P account quota at ${remaining} (threshold ${OP_PREFLIGHT_MIN_REMAINING}); skipping $(basename "${0:-unknown}")"
        echo "1Password account quota is down to ${remaining} (threshold ${OP_PREFLIGHT_MIN_REMAINING})." >&2
        echo "Skipping this run so the 24h window is not kept pinned." >&2
        echo "  Check status: op service-account ratelimit" >&2
        echo "  Run anyway  : OP_QUOTA_GATE_BYPASS=1 $0" >&2
        exit 0
    fi
}
