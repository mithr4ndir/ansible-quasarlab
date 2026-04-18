# Requirements Document

## Introduction

The collector side of the `1password-quota-monitoring` feature. The alert rules, Grafana panel, and runbook live in `k8s-argocd` (see that repo's spec of the same name); this spec covers the Ansible role that runs `op service-account ratelimit` on a schedule, parses the output, and writes Prometheus textfile metrics that the existing node_exporter on command-center1 scrapes.

## Alignment with Product Vision

Homelab prime directive: "everything must be managed in code (IaC), no manual configuration." This role codifies the quota collector so the monitoring surface for 1Password usage is reproducible and versioned alongside the other collectors already deployed via Ansible (unattended_upgrades, k8s_maintenance).

## Requirements

### Requirement 1: New role `op_quota_collector`

**User Story:** As an operator, I want a reusable Ansible role that installs and schedules the 1Password quota collector on command-center1, so that reprovisioning the host does not require re-setting up the monitor.

#### Acceptance Criteria

1. WHEN the role is added to `playbooks/cmd_center.yml` THEN running that playbook SHALL idempotently deploy the collector script, the parser helper, a systemd oneshot service, and a 15-minute timer.
2. WHEN the role is applied THEN it SHALL install files at stable paths: `/usr/local/bin/op-quota-collector.sh` (0755 root), `/usr/local/lib/op-quota-collector/parse.py` (0755 root), `/etc/systemd/system/op-quota-collector.service`, `/etc/systemd/system/op-quota-collector.timer`.
3. WHEN a role file changes THEN systemd SHALL reload (via a handler, not unconditionally) and the timer SHALL stay enabled.
4. WHEN the role runs the first time THEN it SHALL trigger one collector run so the first Prometheus scrape after deploy has data.

### Requirement 2: Collector script behavior

**User Story:** As the alerting system, I need the collector to emit consistent metrics whether or not `op` succeeds, so that stale data is detectable via a dedicated health metric rather than silently missing series.

#### Acceptance Criteria

1. WHEN the collector runs and `op` succeeds THEN it SHALL write metrics `onepassword_ratelimit_{used,limit,remaining,reset_seconds}` with labels `type` and `action`, plus `onepassword_ratelimit_collector_success 1`.
2. WHEN the kill switch (`/var/lib/ansible-quasarlab/1p-killswitch`) is active THEN the collector SHALL NOT call `op`, SHALL preserve any existing metric values for the main gauges, SHALL set `onepassword_ratelimit_collector_success 0`, and SHALL record a distinct syslog line (`reason=killswitch`).
3. WHEN `op` returns a rate-limit error THEN the collector SHALL trip the kill switch via `op_killswitch_scan_file`, write `collector_success 0`, preserve the previous metric values, and exit 0.
4. WHEN any other failure occurs (parse error, file-write error, op CLI missing) THEN the collector SHALL log to syslog with a distinct reason string, write `collector_success 0`, preserve previous metric values, and exit 0.
5. WHEN the collector writes the metric file THEN it SHALL use an atomic `.tmp` + rename so Prometheus never reads a partial file.

### Requirement 3: Parser correctness

**User Story:** As the collector, I need a small parser I can hand the `op` table and get back valid Prometheus text, so the shell code stays simple and the parser can be regression-tested.

#### Acceptance Criteria

1. WHEN the parser receives the standard 3-row table (token/write, token/read, account/read_write) THEN it SHALL emit exactly six metric lines per row (used, limit, remaining, reset_seconds if present) with correct labels.
2. WHEN a `RESET` cell is "N/A" THEN the parser SHALL omit the `reset_seconds` line for that row rather than emit a sentinel value.
3. WHEN a `RESET` cell is "X hours from now", "X minutes from now", or "X hours and Y minutes from now" THEN the parser SHALL convert correctly to seconds (e.g. "5 hours from now" = 18000, "23 hours and 59 minutes from now" = 86340).
4. WHEN the parser output is consumed by `promtool check metrics` THEN it SHALL pass without warnings.
5. WHEN the parser receives malformed input THEN it SHALL exit non-zero with a human-readable error on stderr and produce no output on stdout.

### Requirement 4: Cadence and resource cost

**User Story:** As an operator, I want the collector itself to consume a small, predictable slice of the daily quota so it cannot itself become a pinning cause.

#### Acceptance Criteria

1. WHEN the timer fires THEN it SHALL fire every 15 minutes (`OnCalendar=*:0/15`).
2. WHEN the collector runs once THEN it SHALL consume exactly 1 request against the daily account limit (the single `op service-account ratelimit` call).
3. WHEN the timer schedule is computed for 24 hours THEN total consumption SHALL be 96 requests/day, which is 9.6% of the 1Password Families 1000/day cap.

## Non-Functional Requirements

### Code Architecture and Modularity

- Role structure follows the same shape as `roles/k8s_maintenance`: `tasks/main.yml`, `templates/`, `files/`, `defaults/main.yml`, `tests/` for parser fixtures.
- Shell script sources `scripts/lib/op-killswitch.sh`. Parser is plain Python 3 stdlib (no pip deps).
- No role-specific secrets. Reuses `OP_SERVICE_ACCOUNT_TOKEN` from the standard `~/.config/op/service-account-token` path the other wrappers already use.

### Performance

- Collector cold run (including `op` round trip): under 5 seconds.
- Parser: under 50 ms for the standard 3-row table.
- Total CPU cost: negligible (bounded oneshot).

### Reliability

- Collector always exits 0. Failure is surfaced via `onepassword_ratelimit_collector_success 0` + syslog, never a systemd unit failure. This avoids a flapping `.service` unit that could pollute `systemctl --failed`.
- `.tmp` + rename write pattern for the metric file.
- Integrates with the kill switch so a rate-limit event during the collector's own op call does not create a retry storm.

### Testability

- Parser has fixture-based unit tests under `roles/op_quota_collector/tests/` runnable with plain `python3 -m unittest` or `pytest`. Fixtures cover: all-clean state, all-exhausted state, missing reset rows, bad input.
- Shell script: `bash -n` syntax check in CI (out of scope for this role, but the script is small enough to review directly).

### Observability

- Every execution writes at least one log line via `logger -t op-quota-collector`, with a structured `reason=` tag (`success`, `killswitch`, `ratelimited`, `parse_error`, `op_missing`, `write_error`).
