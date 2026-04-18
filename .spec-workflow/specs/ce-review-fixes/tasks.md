# Tasks: Compound-Engineering Review Fixes (PRs #107, #109)

Progress tracked as checkboxes. Each task corresponds to a specific `safe_auto` finding from the 2026-04-18 `/ce:review` run.

## Branch feat/op-quota-collector (PR #109)

- [x] **Fix #1** Remove committed `tests/__pycache__/test_parse.cpython-312.pyc`; add `__pycache__/` and `*.pyc` to repo `.gitignore`
- [x] **Fix #2** Install `op-killswitch.sh` into `/usr/local/lib/op-quota-collector/` from the role; update `op_quota_killswitch_lib` default
- [x] **Fix #3** Fix `finish()` grep to strip `# HELP`/`# TYPE` comment lines for footer gauges
- [x] **Fix #4** Wrap `op service-account ratelimit` call with `timeout 30`; trip kill switch on exit 124
- [x] **Fix #5** Replace `ansible_env.HOME` in `op_quota_token_file` default with literal `/home/{{ op_quota_user }}`
- [x] **Fix #6** Add TYPE/ACTION allowlist in `parse.py::emit()` to prevent label injection
- [x] **Fix #7** Scan combined stdout+stderr for kill-switch markers (not stderr alone)
- [x] **Fix #8** Gate prime-run Ansible task on `stat.exists` check of metric file
- [x] **Fix #9** Broaden `parse_reset()` regex to handle "N seconds", "Never"; fallback `None` on unknown shapes
- [x] **Fix #10** Inline `OP_SERVICE_ACCOUNT_TOKEN` export to the `op` call only, remove global export
- [x] Run `python3 -m unittest roles/op_quota_collector/tests/test_parse.py -v` - all 5 tests pass
- [ ] Commit fixes as single `fix(op-quota-collector): address ce:review P1/P2 findings` commit
- [ ] Push to `origin/feat/op-quota-collector`

## Branch feat/etcd-defrag-timer (PR #107)

- [ ] **Fix #11** Wrap every `kubectl exec ... etcdctl` call in `etcd-defrag.sh.j2` with `timeout 60`
- [ ] Commit fix as `fix(etcd-defrag): add 60s timeout to kubectl exec calls`
- [ ] Push to `origin/feat/etcd-defrag-timer`

## Verification

- [ ] PR #109 CI green (GitHub Actions, any ansible-lint or yaml-lint steps)
- [ ] PR #107 CI green
- [ ] No em dashes in any changed file
- [ ] Both PRs still pointed at `main` and not drifted

## Follow-ups (captured, NOT done here)

- [ ] Open issue: role rename `op_quota_collector` -> `op_ratelimit_collector` (cosmetic, keep backward-compatible alias)
- [ ] Open issue: handler cascade cleanup in `op_quota_collector/handlers/main.yml`
- [ ] Open issue: compaction-error stderr scanning in `etcd-defrag.sh.j2`
- [ ] Post-merge: scale `external-secrets` back to 1 replica
- [ ] Post-merge: redeploy collector so it picks up new `op-killswitch.sh` path
