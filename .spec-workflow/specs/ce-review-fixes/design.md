# Design: Compound-Engineering Review Fixes (PRs #107, #109)

## Fix Application Order

Apply in a deterministic order on each branch, test locally, commit once per branch, force push if needed.

### Branch feat/op-quota-collector (10 fixes)

1. **Delete stale .pyc + harden .gitignore** (Fix #1).
   - `git rm --cached roles/op_quota_collector/tests/__pycache__/test_parse.cpython-312.pyc`
   - Append `__pycache__/` and `*.pyc` to repo root `.gitignore` if absent.

2. **Relocate killswitch lib** (Fix #2).
   - Add task to copy `scripts/lib/op-killswitch.sh` into `/usr/local/lib/op-quota-collector/op-killswitch.sh` with mode 0755.
   - Change default `op_quota_killswitch_lib` to `/usr/local/lib/op-quota-collector/op-killswitch.sh`.
   - Template still sources `$KILLSWITCH_LIB` so other consumers can override.

3. **Fix finish() grep to strip HELP/TYPE comments** (Fix #3).
   - Current regex only strips metric lines. Add alternation for `^# (HELP|TYPE) onepassword_ratelimit_collector_(success|last_success_timestamp_seconds)\b`.

4. **Timeout on op call** (Fix #4).
   - Wrap `op service-account ratelimit` invocation with `timeout 30`.
   - Trip kill switch on exit 124 as well.

5. **Literal home path** (Fix #5).
   - Change default `op_quota_token_file` from `{{ ansible_env.HOME | default('/home/' + op_quota_user) }}/.config/...` to `/home/{{ op_quota_user }}/.config/...`.

6. **TYPE/ACTION allowlist in parse.py** (Fix #6).
   - In `emit()`, validate `action` and `type` match `^[a-z_]+$` before rendering.
   - On mismatch, skip the row and write stderr warning.

7. **Dual-stream kill-switch scan** (Fix #7).
   - Tee `op` stdout and stderr together, scan combined output for rate-limit markers.

8. **Prime-run gated by stat** (Fix #8).
   - `ansible.builtin.stat` on metric file.
   - Run prime-run command only when `not stat.stat.exists`.

9. **Parse_reset widened regex** (Fix #9).
   - Support "N seconds", "Never", return `None` when unknown.

10. **Inline token export** (Fix #10).
    - Change `export OP_SERVICE_ACCOUNT_TOKEN=...; op ...` to `OP_SERVICE_ACCOUNT_TOKEN=... op ...`.

Run unit tests. Commit. Push.

### Branch feat/etcd-defrag-timer (1 fix)

11. **kubectl timeouts** (Fix #11).
    - Wrap every `kubectl exec ... etcdctl` with `timeout 60`.
    - Handle exit 124 with explicit stderr note in log.

Commit. Push.

## Testing Approach

### Local validation (no 1Password calls)

- Python: `cd roles/op_quota_collector && python3 -m unittest tests/test_parse.py -v`
- Shell: `shellcheck templates/op-quota-collector.sh.j2` (treat templated vars as stubs) and `shellcheck templates/etcd-defrag.sh.j2`
- Ansible: `ansible-playbook playbooks/op_quota.yml --check --diff` against `cmd_center` host only if we are confident handlers won't actually run `op`.

### Explicit non-tests (do NOT run)

- Do not invoke `op service-account ratelimit` during fix verification. Quota is precious.
- Do not run the full `cmd_center.yml` playbook until both PRs merge.

## Risks

- Moving killswitch lib to `/usr/local/lib/` means the existing deployed collector (from pre-fix template) still points at the old path. Must redeploy once merged. Captured in post-merge runbook.
- `timeout 124` exit handling: bash wrappers must check `$?` immediately before any other command, else `$?` gets overwritten. Use `status=$?; case "$status" in ...`.
- Regex widening in `parse_reset()` must keep existing fixtures passing.

## Out of scope for this spec

- Role rename `op_quota_collector` -> `op_ratelimit_collector` (cosmetic, defer to follow-up).
- Handler cascade cleanup (reload-systemd -> restart-timer chain reliability, gated_auto).
- Multi-host rollout strategy (covered separately).
