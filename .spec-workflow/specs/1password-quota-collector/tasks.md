# Tasks Document

- [ ] 1. Create parse.py helper
  - File: roles/op_quota_collector/files/parse.py
  - Implement table-to-Prometheus-textfile conversion
  - Handle "N/A", "X hours from now", "X minutes from now", "X hours and Y minutes from now" reset values
  - Exit 2 with stderr on malformed input
  - Python 3 stdlib only (re, sys)
  - Purpose: Testable parser separated from shell orchestration
  - _Leverage: None (new component)_
  - _Requirements: 3.1-3.5_

- [ ] 2. Create parser regression tests
  - Files:
    - roles/op_quota_collector/tests/fixtures/clean.txt
    - roles/op_quota_collector/tests/fixtures/clean.expected.prom
    - roles/op_quota_collector/tests/fixtures/exhausted.txt
    - roles/op_quota_collector/tests/fixtures/exhausted.expected.prom
    - roles/op_quota_collector/tests/fixtures/all_na.txt
    - roles/op_quota_collector/tests/fixtures/all_na.expected.prom
    - roles/op_quota_collector/tests/fixtures/malformed_header.txt (no expected, should exit 2)
    - roles/op_quota_collector/tests/test_parse.py (pytest or unittest)
  - Purpose: Catch parser regressions before deploy
  - _Leverage: None_
  - _Requirements: 3.1-3.5, NFR testability_

- [ ] 3. Create op-quota-collector.sh template
  - File: roles/op_quota_collector/templates/op-quota-collector.sh.j2
  - Source scripts/lib/op-killswitch.sh
  - Call op_killswitch_check_or_exit at top (before any op call)
  - Load OP_SERVICE_ACCOUNT_TOKEN from ~/.config/op/service-account-token
  - Run `op service-account ratelimit`, capture stdout + stderr separately
  - On op error, call op_killswitch_scan_file on stderr
  - Pipe stdout through parse.py, append collector_success + collector_timestamp_seconds
  - Atomic .tmp + rename write
  - Always exit 0
  - Tag every path with `logger -t op-quota-collector reason=<tag>`
  - Purpose: Orchestrate one measurement cycle with full error handling
  - _Leverage: scripts/lib/op-killswitch.sh_
  - _Requirements: 2.1-2.5_

- [ ] 4. Create systemd service + timer templates
  - Files:
    - roles/op_quota_collector/templates/op-quota-collector.service.j2
    - roles/op_quota_collector/templates/op-quota-collector.timer.j2
  - Service: Type=oneshot, User={{ op_quota_user }}, ExecStart=/usr/local/bin/op-quota-collector.sh
  - Timer: OnCalendar={{ op_quota_schedule }}, Persistent=true, AccuracySec=30s
  - Purpose: Scheduled oneshot execution surviving reboots
  - _Leverage: roles/k8s_maintenance/templates/etcd-defrag.service.j2 as shape reference_
  - _Requirements: 4.1, 4.3_

- [ ] 5. Wire role tasks + defaults
  - Files:
    - roles/op_quota_collector/tasks/main.yml
    - roles/op_quota_collector/handlers/main.yml
    - roles/op_quota_collector/defaults/main.yml
  - Deploy parser, script, service, timer with handlers for daemon-reload and restart
  - Prime run at end of role so first metric scrape has data
  - Purpose: Idempotent deployment of the whole collector
  - _Leverage: roles/k8s_maintenance/tasks/main.yml_
  - _Requirements: 1.1-1.4_

- [ ] 6. Add role to playbooks/cmd_center.yml
  - File: playbooks/cmd_center.yml
  - Append op_quota_collector after existing roles
  - Purpose: Role is applied in the scheduled ansible run
  - _Leverage: existing cmd_center.yml structure_
  - _Requirements: 1.1_

- [ ] 7. Open PR in ansible-quasarlab
  - Title: "feat(collector): 1Password quota collector role"
  - Reference this spec and the sibling k8s-argocd spec
  - Purpose: Ship the collector so the k8s-argocd alerts have a data source
  - _Leverage: recent PRs (#104, #107) as PR shape reference_
  - _Requirements: all (delivery vehicle)_

- [ ] 8. After merge, verify live
  - Apply via run-proxmox.sh (or scheduled timer)
  - `systemctl status op-quota-collector.timer` on command-center1: enabled + active
  - `cat /var/lib/node_exporter/textfiles/op_quota.prom` shows valid metrics
  - `journalctl -t op-quota-collector -n 5` shows `reason=success`
  - Confirm k8s-argocd Prometheus query returns data within a scrape cycle
  - Purpose: End-to-end confirmation the collector pipeline works in production
  - _Leverage: operational verification pattern from etcd defrag timer deploy_
  - _Requirements: all acceptance criteria_
