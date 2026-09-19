#!/usr/bin/env bash
# Links from the Ansible alerts to the ARA report of the run behind them.
#
# Sourced by run-proxmox.sh and run-security.sh:
#
#   ara_tag_run proxmox          # before the first ansible-playbook
#   ...run playbooks...
#   links=$(ara_run_link_metrics "${!playbook_results[@]}")
#   # then print "$links" inside the same atomic .prom write
#
# ara_tag_run gives every ansible-playbook in this wrapper run one ARA label,
# run:<name>:<uuid>, through the callback's ARA_DEFAULT_LABELS. The lookup then
# asks ARA for exactly that label. "Newest playbook called vm_baseline.yml"
# would pick the wrong run whenever a manual run overlaps a timer run.
#
# Fails open everywhere: ARA down, slow, or missing a record means no
# ansible_playbook_last_run_info series, and the alert then carries no link at
# all rather than a generic one. It never changes the wrapper's exit status or
# its other metrics.
#
# Failed runs are linked as well: ARA records them, and the wrappers call the
# lookup after the playbook loop, which does not exit early on a non-zero
# ansible-playbook. A run that dies before its first play starts is the
# exception, since the callback attaches labels at play start.

# The one place the ARA address lives. Used for the API call (the server
# listens on 0.0.0.0:8000 on this host) and for the links in the alerts, which
# have to open from a browser on the LAN.
ARA_BASE_URL="${ARA_BASE_URL:-http://192.168.1.88:8000}"
# Upper bound for the whole lookup, network included. Anything but a positive
# integer falls back to 10: `timeout 0` would mean no bound at all.
ARA_LOOKUP_TIMEOUT_SECS="${ARA_LOOKUP_TIMEOUT_SECS:-10}"
[[ "$ARA_LOOKUP_TIMEOUT_SECS" =~ ^[1-9][0-9]?$ ]] || ARA_LOOKUP_TIMEOUT_SECS=10

_ARA_RUN_LINKS_PY="$(dirname "${BASH_SOURCE[0]}")/ara-run-links.py"

# ara_tag_run NAME
# Exports ARA_DEFAULT_LABELS with a label unique to this run and sets
# ARA_RUN_LABEL. Keeps any labels already in ARA_DEFAULT_LABELS.
ara_tag_run() {
    local run_id
    run_id=$(cat /proc/sys/kernel/random/uuid 2>/dev/null) || run_id=""
    if [[ ! "$run_id" =~ ^[0-9a-f-]{36}$ ]]; then
        echo "ara-run-links: no run id available; this run will not be linked" >&2
        ARA_RUN_LABEL=""
        return 0
    fi
    ARA_RUN_LABEL="run:${1}:${run_id}"
    export ARA_DEFAULT_LABELS="${ARA_DEFAULT_LABELS:+${ARA_DEFAULT_LABELS},}${ARA_RUN_LABEL}"
}

# ara_run_link_metrics PLAYBOOK...
# Prints ansible_playbook_last_run_info lines (with HELP/TYPE) for the given
# playbooks of this run, or nothing. Always returns 0. Diagnostics go to stderr.
ara_run_link_metrics() {
    local out
    [[ -n "${ARA_RUN_LABEL:-}" && $# -gt 0 ]] || return 0
    # timeout is the hard bound. The helper's socket timeout, half of it, is
    # the polite one that lets it fail with a reason first. -I keeps the ARA
    # venv on PYTHONPATH (from ara-ansible-env.sh) out of the helper, which
    # needs only the standard library.
    if out=$(timeout --kill-after=2 "$ARA_LOOKUP_TIMEOUT_SECS" \
            python3 -I "$_ARA_RUN_LINKS_PY" \
            --base-url "$ARA_BASE_URL" \
            --label "$ARA_RUN_LABEL" \
            --timeout "$(( (ARA_LOOKUP_TIMEOUT_SECS + 1) / 2 ))" \
            "$@"); then
        [[ -n "$out" ]] && printf '%s\n' "$out"
    else
        echo "ara-run-links: lookup under ${ARA_RUN_LABEL} failed or timed out; no ARA links this run" >&2
    fi
    return 0
}
