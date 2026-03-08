#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="/home/ladino/code/ansible-quasarlab"
LOG_DIR="/var/log/ansible-quasarlab"
LOGFILE="${LOG_DIR}/proxmox-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "$LOG_DIR"

cd "$REPO_DIR"

# Pull latest
git pull --ff-only origin main >> "$LOGFILE" 2>&1

# Run the playbook (don't exit on failure — we still need to rotate logs)
ansible-playbook playbooks/proxmox.yml --diff >> "$LOGFILE" 2>&1
exit_code=$?

# Keep only last 50 log files
ls -t "$LOG_DIR"/proxmox-*.log 2>/dev/null | tail -n +51 | xargs -r rm --

exit $exit_code
