# Requirements: Compound-Engineering Review Fixes (PRs #107, #109)

## Context

On 2026-04-18, the compound-engineering `/ce:review` skill was run against two open PRs in `ansible-quasarlab`:

- **PR #107** (`feat/etcd-defrag-timer`): weekly etcd compact + defrag systemd timer under `roles/k8s_maintenance`
- **PR #109** (`feat/op-quota-collector`): Prometheus textfile collector for 1Password daily quota under `roles/op_quota_collector`

Seven reviewer personas returned structured findings. Synthesis produced 2 P1, 8 P2, 5 P3 items plus residual risks. Both PRs were verdicted "Ready with fixes" - mergeable after applying `safe_auto` items.

## Goal

Apply all 11 `safe_auto` fixes across both branches so PR #107 and PR #109 can be merged cleanly. Track each fix as a discrete, completable task and preserve audit trail in `.spec-workflow/`.

## Non-Goals

- Gated-auto or manual-owner findings (e.g. role rename `op_quota_collector` -> `op_ratelimit_collector`, compaction-error stderr scanning). Those are deferred to follow-up issues.
- Changes outside these two branches. Merges happen in separate operation.
- ESO scale-back to 1 replica. That is a post-merge operational step.

## Requirements

### R1: No secret/quota leaks from fixes themselves
All fixes must be applied without running `op` CLI or consuming quota during verification. Daily 1Password cap is 1000 reads and is shared across all service accounts.

### R2: No em dashes
Per global CLAUDE.md, no em dashes anywhere in code, comments, commit messages, PR descriptions, or spec files.

### R3: Idempotent and rerunnable
All fixes must be safe to apply twice. Ansible tasks stay declarative. No destructive changes to host state beyond what the role already owned.

### R4: Tests pass before commit
`roles/op_quota_collector/tests/test_parse.py` must pass after every parser change. Etcd defrag script must be shellcheck-clean.

### R5: Each fix is a discrete commit on its branch
`feat/op-quota-collector` gets one commit bundling its 10 fixes. `feat/etcd-defrag-timer` gets one commit for its single fix. Both push to the existing open PRs.

### R6: Spec tracks state
`.spec-workflow/specs/ce-review-fixes/tasks.md` lists every fix as a checkbox. Each gets ticked as applied. Completed spec is retained for audit.

## Fix Inventory

### PR #109 (feat/op-quota-collector) - 10 fixes

| # | Severity | Area | Fix |
|---|----------|------|-----|
| 1 | P2 | hygiene | Remove `tests/__pycache__/test_parse.cpython-312.pyc`, add `__pycache__/` + `*.pyc` to repo `.gitignore` |
| 2 | P1 | portability | Move `op-killswitch.sh` install target into the role (ship to `/usr/local/lib/op-quota-collector/op-killswitch.sh`), update default var, stop pointing at `/home/ladino/code/...` |
| 3 | P2 | correctness | Fix `grep` regex in `finish()` to also strip `# HELP`/`# TYPE` lines for the success/timestamp gauges so footer is not duplicated |
| 4 | P2 | reliability | Wrap `op service-account ratelimit` with `timeout 30` so a hung CLI call cannot pin the collector |
| 5 | P2 | portability | Replace `ansible_env.HOME \| default(...)` with literal `/home/{{ op_quota_user }}` in `op_quota_token_file` default (become=yes swaps HOME to /root) |
| 6 | P1 | security | Add TYPE/ACTION allowlist in `parse.py` `emit()` to prevent label injection from crafted CLI output |
| 7 | P2 | reliability | Scan both stdout and stderr for kill-switch markers (`rate-limit`, `429`, etc) - stderr alone missed markers CLI writes to stdout |
| 8 | P2 | hygiene | Gate prime-run task with `stat.exists` check on metric file so daily quota is not burned on every playbook rerun |
| 9 | P2 | correctness | Broaden `parse_reset()` regex to handle "30 seconds", "Never", and fall back to `None` on unknown shapes instead of raising |
| 10 | P3 | hygiene | Inline `OP_SERVICE_ACCOUNT_TOKEN` export to just the `op` call, not shell-wide export |

### PR #107 (feat/etcd-defrag-timer) - 1 fix

| # | Severity | Area | Fix |
|---|----------|------|-----|
| 11 | P2 | reliability | Wrap all `kubectl exec ... etcdctl` calls in `etcd-defrag.sh.j2` with `timeout 60` so a hung API server cannot pin the script |

## Acceptance

- [ ] All 11 checkboxes in `tasks.md` are ticked
- [ ] Both PRs show green CI
- [ ] Parser tests still pass (`python3 -m unittest roles/op_quota_collector/tests/test_parse.py`)
- [ ] No new em dashes introduced
- [ ] Residual risks captured as GitHub issues or follow-up spec entries
