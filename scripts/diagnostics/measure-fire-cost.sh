#!/usr/bin/env bash
# measure-fire-cost.sh
#
# Single-shot diagnostic harness to identify the per-fire 1Password
# read_write cost of `scripts/run-proxmox.sh`.
#
# Why this exists:
#   2026-04-18, 2026-05-02, and 2026-05-06 all saw the account read_write
#   cap exhausted within hours of the ansible timers being re-enabled.
#   Phase 1 vault rollout (#129) did not eliminate the burn; Prometheus
#   shows ~124 reads consumed per `ansible-proxmox.service` fire after
#   #129 merged. The per-fire source has not been pinpointed yet.
#
# What this does:
#   1. Sanity-checks that the cap has enough headroom to safely measure.
#   2. Snapshots `op service-account ratelimit` (free, control-plane).
#   3. Runs run-proxmox.sh under `strace -f -e execve` so every exec
#      across the full fork tree is recorded with timestamps.
#   4. Snapshots cap again and computes the read_write delta.
#   5. Aggregates op execs by subcommand and writes a summary.
#
# How to use:
#   Manual only. Refuses to run from a systemd unit. Best run when
#   `op service-account ratelimit` shows REMAINING >= 200 so a single
#   measurement (expected ~124) cannot exhaust the cap.
#
#   sudo systemctl status ansible-proxmox.timer   # confirm STOPPED
#   ./scripts/diagnostics/measure-fire-cost.sh
#
# Output goes to /tmp/op-fire-cost-<UTC-timestamp>/.
set -euo pipefail

if [[ -n "${INVOCATION_ID:-}" ]]; then
    echo "ERROR: refusing to run inside a systemd unit; this is a manual diagnostic" >&2
    exit 2
fi

if ! command -v strace >/dev/null 2>&1; then
    echo "ERROR: strace not installed; sudo apt install strace" >&2
    exit 2
fi

if ! command -v op >/dev/null 2>&1; then
    echo "ERROR: 1Password CLI not on PATH" >&2
    exit 2
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_PROXMOX="${REPO_DIR}/scripts/run-proxmox.sh"
if [[ ! -x "$RUN_PROXMOX" ]]; then
    echo "ERROR: ${RUN_PROXMOX} not executable" >&2
    exit 2
fi

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${OUT_DIR:-/tmp/op-fire-cost-${RUN_ID}}"
mkdir -p "$OUT_DIR"
TRACE="${OUT_DIR}/strace.log"
RUNLOG="${OUT_DIR}/run.log"
TIMELINE="${OUT_DIR}/timeline.txt"
SUMMARY="${OUT_DIR}/summary.txt"

# 1) Cap pre-snapshot. `op service-account ratelimit` is free (verified
# 2026-04-19, see feedback_op_rate_limit_care.md).
echo "=== PRE: 1P ratelimit ($(date -u +%FT%TZ)) ==="
op service-account ratelimit | tee "${OUT_DIR}/cap-before.txt"

remaining_before=$(awk '/^account/ && $2 == "read_write" {print $5}' "${OUT_DIR}/cap-before.txt")
if [[ -z "$remaining_before" ]]; then
    echo "ERROR: could not parse account/read_write REMAINING from cap-before.txt" >&2
    exit 3
fi

if (( remaining_before < 200 )); then
    echo "ABORT: account read_write REMAINING=${remaining_before} is below the 200 floor." >&2
    echo "       A single fire historically costs ~124 reads; running now risks exhausting the cap." >&2
    echo "       Wait for the rolling 24h window to free up more headroom." >&2
    exit 4
fi

{
    echo "run-id: ${RUN_ID}"
    echo "trace-start: $(date -u +%FT%TZ)"
    echo "remaining-before: ${remaining_before}"
} | tee "$TIMELINE"

# 2) Run the production fire under strace. -f follows forks so the full
# tree of ansible-playbook -> python -> child processes is captured.
# -e trace=execve only records exec calls, keeping the trace small even
# across an 8-playbook sequence. -tt gives microsecond timestamps so we
# can correlate op invocations with which playbook was running.
# -s 256 truncates argv strings late enough to keep subcommands intact.
echo
echo "=== RUN: strace -f -e execve -- run-proxmox.sh ==="
set +e
strace -f -tt -s 256 -e trace=execve -o "$TRACE" \
    -- "$RUN_PROXMOX" >"$RUNLOG" 2>&1
run_rc=$?
set -e

{
    echo "trace-end: $(date -u +%FT%TZ)"
    echo "run-rc: ${run_rc}"
} | tee -a "$TIMELINE"

# 3) Settle window. The collector polls every 5 min, but the live ratelimit
# call reflects the rolling counters within seconds. A short pause guards
# against any in-flight ESO/fork that fires after run-proxmox returns.
sleep 5

echo
echo "=== POST: 1P ratelimit ($(date -u +%FT%TZ)) ==="
op service-account ratelimit | tee "${OUT_DIR}/cap-after.txt"
remaining_after=$(awk '/^account/ && $2 == "read_write" {print $5}' "${OUT_DIR}/cap-after.txt")
delta=$(( remaining_before - remaining_after ))

# 4) Aggregate op execs. The exec line in strace looks like:
#   <pid>  HH:MM:SS.uuuuuu execve("/usr/bin/op", ["op", "read", "..."], ...) = 0
# We only count calls that actually started (returned 0); failed execves
# (no such file, etc.) do not consume cap.
op_lines=$(grep -E 'execve\("[^"]*/op", \["op",' "$TRACE" 2>/dev/null | grep -E '\) = 0$' || true)
op_total=$(printf '%s\n' "$op_lines" | sed '/^$/d' | wc -l | tr -d ' ')

# Subcommand breakdown: pull argv[1] out of each line.
op_subcmd_breakdown=$(printf '%s\n' "$op_lines" \
    | sed -nE 's/.*execve\("[^"]*\/op", \["op", "([^"]+)".*/\1/p' \
    | sort | uniq -c | sort -rn)

# Per-pid breakdown (helps see whether one process burst-called or many forks did).
op_pid_breakdown=$(printf '%s\n' "$op_lines" \
    | sed -nE 's/^([0-9]+) +.*/\1/p' \
    | sort | uniq -c | sort -rn | head -20)

# 5) Write summary.
{
    echo "=== measure-fire-cost summary ==="
    echo "run-id:                ${RUN_ID}"
    echo "run-proxmox rc:        ${run_rc}"
    echo "cap remaining BEFORE:  ${remaining_before}"
    echo "cap remaining AFTER:   ${remaining_after}"
    echo "cap delta (used):      ${delta}"
    echo
    echo "op execve calls (rc=0): ${op_total}"
    echo
    echo "op subcommand breakdown:"
    if [[ -n "$op_subcmd_breakdown" ]]; then
        printf '%s\n' "$op_subcmd_breakdown" | sed 's/^/  /'
    else
        echo "  (none)"
    fi
    echo
    echo "top op-calling pids (count, pid):"
    if [[ -n "$op_pid_breakdown" ]]; then
        printf '%s\n' "$op_pid_breakdown" | sed 's/^/  /'
    else
        echo "  (none)"
    fi
    echo
    echo "discrepancy check:"
    if [[ $op_total -eq $delta ]]; then
        echo "  OK: op execs match cap delta exactly"
    else
        echo "  WARN: op execs (${op_total}) != cap delta (${delta})."
        echo "  Possible explanations:"
        echo "    - free op subcommands (service-account ratelimit, --version) inflate exec count."
        echo "    - other 1P consumers fired during the run (ESO refreshes, dashboards)."
        echo "    - cache-served reads incur an exec but no cap charge (op-secret-cache.sh hits)."
    fi
    echo
    echo "files:"
    echo "  ${TRACE} ($(stat -c%s "$TRACE" 2>/dev/null | numfmt --to=iec 2>/dev/null || echo '?'))"
    echo "  ${RUNLOG} ($(stat -c%s "$RUNLOG" 2>/dev/null | numfmt --to=iec 2>/dev/null || echo '?'))"
    echo
    echo "next steps:"
    echo "  1. open ${TRACE} and grep around the op execve lines to see the parent"
    echo "     ansible-playbook process and which playbook was running."
    echo "  2. if the burn is dominated by op calls from a single role, inline-vault"
    echo "     that secret following docs/vault.md and re-run this harness."
    echo "  3. once delta < 10 across two consecutive fires, consider re-enabling"
    echo "     the timers (and add a pre-flight gate so this cannot regress)."
} | tee "$SUMMARY"

echo
echo "Outputs in: ${OUT_DIR}"
exit "$run_rc"
