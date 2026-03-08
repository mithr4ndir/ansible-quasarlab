#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="/home/ladino/code/ansible-quasarlab"
LOG_DIR="/var/log/ansible-quasarlab"
LOGFILE="${LOG_DIR}/ansible-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "$LOG_DIR"

cd "$REPO_DIR"

# Pull latest
git pull --ff-only origin main >> "$LOGFILE" 2>&1

exit_code=0

# Run playbooks (don't exit on failure — we still need to rotate logs)
for playbook in proxmox.yml monitoring.yml; do
    echo "=== Running ${playbook} ===" >> "$LOGFILE"
    ansible-playbook "playbooks/${playbook}" --diff >> "$LOGFILE" 2>&1
    rc=$?
    if [[ $rc -ne 0 ]]; then
        exit_code=$rc
    fi
done

# Keep only last 50 log files
ls -t "$LOG_DIR"/ansible-*.log 2>/dev/null | tail -n +51 | xargs -r rm --

exit $exit_code
