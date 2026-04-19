# Tasks: cmd-center1 DR Provisioning

## Phase 1: Refactor existing role

- [x] 1. Extract existing logic into topical task files (PR #118)
  - Split `roles/cmd_center/tasks/main.yml` into `packages.yml`, `kubeconfig.yml`, `ansible_timers.yml`
  - Rewrite `main.yml` as a pure orchestrator using `import_tasks`
  - All 14 original tasks preserved, each file tagged for selective runs
  - `ansible-playbook --syntax-check` passes
  - Open: verify with `--check` mode against live cmd-center1 (post-merge)
  - _Requirements: 2_

## Phase 2: New task files (dependencies first)

- [x] 2. Add CLI tools installation
  - File: `roles/cmd_center/tasks/cli_tools.yml`
  - jq installed via apt
  - gh via official cli.github.com apt repo (keyring in /etc/apt/keyrings)
  - terraform via hashicorp apt repo (keyring in /etc/apt/keyrings)
  - helm as pinned versioned binary in /usr/local/bin with a `helm` symlink
  - yq as pinned versioned binary in /usr/local/bin with a `yq` symlink
  - `op` is not installed here; the existing `onepassword_cli` role in the playbook handles it
  - Final verify step runs each tool's `--version` to confirm it is on PATH
  - _Requirements: 1_

- [ ] 3. Add 1Password service account token deployment
  - File: `roles/cmd_center/tasks/onepassword_token.yml`
  - Create the ansible-vault encrypted variable `op_service_account_token` in `group_vars/cmd_center/vault.yml`
  - Task copies the decrypted token to `/home/{{ ansible_user }}/.op_service_account_token`, mode `0600`, `no_log: true`
  - Verify `op --version` works after deploy
  - _Requirements: 3_

- [ ] 4. Add SSH key deployment via 1P
  - File: `roles/cmd_center/tasks/ssh_keys.yml`
  - Use `op read` to fetch the SSH private key from 1P Infrastructure vault
  - Write to `~/.ssh/id_ed25519` with mode `0600`
  - Write the public key to `~/.ssh/id_ed25519.pub` with mode `0644`
  - Configure `~/.ssh/known_hosts` with `github.com` host key (ssh-keyscan)
  - `no_log: true` on all secret-handling tasks
  - _Requirements: 3_

- [x] 5. Add systemd linger enablement
  - File: `roles/cmd_center/tasks/linger.yml`
  - Stat check on `/var/lib/systemd/linger/{{ ansible_user }}` guards idempotency
  - `loginctl enable-linger` runs with `become: true` when marker missing
  - _Requirements: 5_

## Phase 3: New task files (application layer)

- [x] 6. Add git repo cloning
  - File: `roles/cmd_center/tasks/git_repos.yml`
  - Loops over `lab_repos`; claude-config cloned first as a distinct task, then the rest
  - `update: false` protects local commits
  - Target directory: `/home/{{ ansible_user }}/code/{{ item.name }}`
  - _Requirements: 1, 2_

- [x] 7. Add Claude config bootstrap
  - File: `roles/cmd_center/tasks/claude_bootstrap.yml`
  - Runs `~/code/claude-config/bin/bootstrap.sh`
  - Asserts both `~/.claude/memory` and `~/.claude/projects/-home-{user}/memory` are symlinks pointing at `~/code/claude-config/memory`
  - Fails fast with a clear message if claude-config was not cloned first
  - _Requirements: 4_

- [x] 8. Add Node 22 standalone install
  - File: `roles/cmd_center/tasks/node_runtime.yml`
  - Unarchive with `creates:` for idempotency, download skipped when target exists
  - `current` symlink maintained with `state: link force: true`
  - Version verification step uses `failed_when` to assert expected v{{ node_version }}
  - _Requirements: 1, 2_

- [x] 9. Add spec-workflow dashboard service
  - File: `roles/cmd_center/tasks/spec_workflow.yml` and `templates/spec-workflow-dashboard.service.j2`
  - User-scoped systemd unit, templated port and bind address from defaults
  - Flushes handlers before enable so a fresh unit is picked up on first deploy
  - User systemd handlers added with XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS environment
  - _Requirements: 1, 5_

## Phase 4: Orchestration and variables

- [x] 10. Rewrite main.yml as orchestrator (partial)
  - File: `roles/cmd_center/tasks/main.yml`
  - Currently orchestrates: packages, kubeconfig, ansible_timers, linger, node_runtime, spec_workflow
  - Pending addition: cli_tools, onepassword_token, ssh_keys, git_repos, claude_bootstrap (later phases)
  - _Requirements: 1_

- [x] 11. Update defaults (partial)
  - File: `roles/cmd_center/defaults/main.yml`
  - Added: `node_version`, `node_install_dir`, `spec_workflow_port`, `spec_workflow_bind_address`, `spec_workflow_allow_external_access`, `spec_workflow_cors_enabled`
  - Pending: `lab_repos` (added with git_repos.yml later)
  - _Requirements: 1_

- [x] 12. Add handler for user systemd reload
  - File: `roles/cmd_center/handlers/main.yml`
  - `Reload user systemd` and `Restart spec-workflow-dashboard` handlers added, both user-scoped with XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS environment
  - _Requirements: 1_

## Phase 5: Secrets and vault setup

- [ ] 13. Create vault file for cmd_center secrets
  - File: `group_vars/cmd_center/vault.yml` (ansible-vault encrypted)
  - Variables: `op_service_account_token` (read-only token for Infrastructure vault)
  - Document vault password retrieval in README
  - _Requirements: 3_

- [x] 14. Document manual prereqs
  - File: `roles/cmd_center/README.md` (new file)
  - Lists Ubuntu 24.04, `ladino` with sudo, SSH access for runner, ansible-vault password, 1P service account token as prereqs
  - Documents every task file's tag, the full install inventory, vault variables, and a DR runbook with RTO target
  - _Requirements: 6_

## Phase 6: Testing and verification

- [ ] 15. Run playbook with --check mode against live cmd-center1
  - `ansible-playbook site.yml --limit cmd-center1 --check --diff`
  - Expected: zero changes reported
  - Fix any idempotency bugs found
  - _Requirements: 2_

- [ ] 16. Run full playbook against live cmd-center1
  - `ansible-playbook site.yml --limit cmd-center1 --ask-vault-pass`
  - Verify spec-workflow dashboard still reachable after run
  - Verify no pods in `media` namespace restarted (should not touch k8s state)
  - Verify memory symlinks intact and writable
  - _Requirements: 1, 2, 4_

- [ ] 17. DR rehearsal: provision a fresh VM
  - Create a fresh Ubuntu 24.04 VM on PVE
  - Perform manual prereqs (user creation, SSH access, vault password)
  - Run playbook against it
  - Measure time from start to fully working state (target: under 30 min)
  - Verify all tools present, dashboard reachable, memory symlinks created
  - Document any gaps found
  - _Requirements: 1, 5, 6_

- [ ] 18. Add optional verification task
  - File: `roles/cmd_center/tasks/verify.yml`
  - Tagged: `tags: [verify, never]` so it only runs when explicitly requested
  - Assertions for: dashboard HTTP 200, kubectl connectivity, memory symlinks, required tools on PATH
  - _Requirements: 1_

## Phase 7: Cleanup and PR

- [ ] 19. Create feature branch and open PR
  - Branch: `feat/cmd-center-dr-provisioning`
  - Commit messages per existing conventional commits style
  - PR body links to this spec
  - _Requirements: (all)_

- [ ] 20. Update project documentation
  - Main README: link to the cmd_center role README
  - Update `docs/` if there is a DR runbook page
  - _Requirements: 6_
