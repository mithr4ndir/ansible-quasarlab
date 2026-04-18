# Tasks: cmd-center1 DR Provisioning

## Phase 1: Refactor existing role

- [ ] 1. Extract existing logic into topical task files
  - Split `roles/cmd_center/tasks/main.yml` into `packages.yml`, `kubeconfig.yml`, `ansible_timers.yml`
  - Rewrite `main.yml` to be a pure orchestrator using `include_tasks`
  - Verify existing functionality still works: run playbook with `--check` mode against live cmd-center1, confirm zero unexpected changes
  - _Requirements: 2_

## Phase 2: New task files (dependencies first)

- [ ] 2. Add CLI tools installation
  - File: `roles/cmd_center/tasks/cli_tools.yml`
  - Install via apt where available: `jq`, `gh` (via hashicorp GPG key + repo), `yq` (binary from GitHub releases if not in apt)
  - Install `helm` via helm.sh script or binary tarball
  - Install `terraform` via hashicorp apt repo
  - Verify `op` is handled by the existing `onepassword_cli` role (include it as a role dependency if needed)
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

- [ ] 5. Add systemd linger enablement
  - File: `roles/cmd_center/tasks/linger.yml`
  - Check `/var/lib/systemd/linger/{{ ansible_user }}` existence first (idempotency guard)
  - Run `loginctl enable-linger {{ ansible_user }}` if not present
  - Requires `become: true`
  - _Requirements: 5_

## Phase 3: New task files (application layer)

- [ ] 6. Add git repo cloning
  - File: `roles/cmd_center/tasks/git_repos.yml`
  - Loop over `lab_repos` variable
  - Clone `claude-config` FIRST, then others (order-sensitive due to claude-config bootstrap)
  - Use `git: update=no` to avoid clobbering local work on re-runs
  - Target directory: `/home/{{ ansible_user }}/code/{{ item.name }}`
  - _Requirements: 1, 2_

- [ ] 7. Add Claude config bootstrap
  - File: `roles/cmd_center/tasks/claude_bootstrap.yml`
  - Run `~/code/claude-config/bin/bootstrap.sh`
  - The script is idempotent: it checks for existing symlinks before creating
  - Verify: `~/.claude/memory` is a symlink to `~/code/claude-config/memory`
  - Verify: `~/.claude/projects/-home-ladino/memory` is a symlink to `~/code/claude-config/memory`
  - _Requirements: 4_

- [ ] 8. Add Node 22 standalone install
  - File: `roles/cmd_center/tasks/node_runtime.yml`
  - Download `node-v{{ node_version }}-linux-x64.tar.xz` from nodejs.org
  - Extract to `/home/{{ ansible_user }}/.local/lib/nodejs/`
  - Create symlink `current` pointing to the versioned directory
  - Use `creates:` argument for idempotency
  - Verify `~/.local/lib/nodejs/current/bin/node --version` returns the expected version
  - _Requirements: 1, 2_

- [ ] 9. Add spec-workflow dashboard service
  - File: `roles/cmd_center/tasks/spec_workflow.yml`
  - Template: `roles/cmd_center/templates/spec-workflow-dashboard.service.j2`
  - Deploy unit to `~/.config/systemd/user/spec-workflow-dashboard.service`
  - Notify handler: systemctl --user daemon-reload + restart
  - Enable and start: `systemd: name=spec-workflow-dashboard scope=user enabled=true state=started`
  - Verify: `curl http://127.0.0.1:5000` returns HTTP 200
  - _Requirements: 1, 5_

## Phase 4: Orchestration and variables

- [ ] 10. Rewrite main.yml as orchestrator
  - File: `roles/cmd_center/tasks/main.yml`
  - Sequence per design.md Orchestration Order section
  - Each line is `- include_tasks: <filename>`
  - Add tags per task file (e.g., `tags: [packages]`, `tags: [node]`) for selective runs
  - _Requirements: 1_

- [ ] 11. Update defaults
  - File: `roles/cmd_center/defaults/main.yml`
  - Add: `node_version`, `node_install_dir`, `lab_repos`, `spec_workflow_port`, `spec_workflow_bind_address`, `spec_workflow_cors_enabled`
  - Preserve existing `ansible_repo_path` variable
  - _Requirements: 1_

- [ ] 12. Add handler for user systemd reload
  - File: `roles/cmd_center/handlers/main.yml`
  - Add handler: `Reload user systemd` running `systemctl --user daemon-reload`
  - Add handler: `Restart spec-workflow-dashboard` running `systemctl --user restart spec-workflow-dashboard`
  - _Requirements: 1_

## Phase 5: Secrets and vault setup

- [ ] 13. Create vault file for cmd_center secrets
  - File: `group_vars/cmd_center/vault.yml` (ansible-vault encrypted)
  - Variables: `op_service_account_token` (read-only token for Infrastructure vault)
  - Document vault password retrieval in README
  - _Requirements: 3_

- [ ] 14. Document manual prereqs
  - File: `roles/cmd_center/README.md` (create if missing) or update main project README
  - List: Ubuntu 24.04 fresh install, `ladino` user with sudo, initial SSH key for Ansible runner, ansible-vault password
  - Reference 1P item paths for SSH keys, op service account token
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
