# Requirements: cmd-center1 DR Provisioning

## Introduction

Currently, cmd-center1 (the host running Claude Code sessions, spec-workflow dashboard, and the Ansible/K8s control plane) is only partially provisioned by the existing `cmd_center` Ansible role. Critical components (Node 22, spec-workflow dashboard, Claude Code memory symlinks, lab repo clones, CLI tools, SSH keys) are configured manually. If the host dies, rebuilding takes hours of manual work and risks losing session continuity.

This spec extends the existing `cmd_center` role so a fresh Ubuntu 24.04 host becomes a fully working command center via a single Ansible playbook run.

## Requirements

### Requirement 1: Fully Automated Provisioning

**User Story:** As a homelab operator, I want a single Ansible playbook run to fully provision cmd-center1, so that I can recover from host failure without manual tool installation.

#### Acceptance Criteria

1. WHEN a fresh Ubuntu 24.04 host is available with user `ladino` and SSH access THEN the `site.yml` playbook SHALL provision it to a fully working state
2. IF the playbook runs successfully THEN all tools (kubectl, helm, yq, jq, gh, terraform, op, ansible, Node 22) SHALL be installed and on PATH
3. WHEN provisioning completes THEN the spec-workflow dashboard SHALL be running and reachable at http://<host>:5000
4. IF the host reboots THEN all services SHALL come back up automatically without manual intervention

### Requirement 2: Idempotency

**User Story:** As a homelab operator, I want to re-run the playbook safely on an already-provisioned host, so that I can apply config updates without fear of breaking existing state.

#### Acceptance Criteria

1. WHEN the playbook runs on a fully provisioned host THEN it SHALL make zero changes (all tasks report `ok`)
2. IF a git repo is already cloned THEN the task SHALL NOT force-update it to avoid clobbering local work
3. WHEN Node 22 is already installed at the expected version THEN the task SHALL skip re-download

### Requirement 3: Secrets Management

**User Story:** As a homelab operator, I want secrets (1Password service account token, SSH keys) handled securely, so that they never appear in plaintext in git or logs.

#### Acceptance Criteria

1. WHEN the 1Password service account token is deployed THEN it SHALL be sourced from ansible-vault or pulled from 1P at runtime
2. IF SSH keys are deployed THEN they SHALL be fetched from 1P using the `op` CLI, not committed to the repo
3. WHEN logs are written THEN secrets SHALL NOT appear in Ansible output (use `no_log: true` where appropriate)

### Requirement 4: Persistent Memory

**User Story:** As a homelab operator, I want Claude Code memory (auto-memory and memory-bank) symlinked to the claude-config git repo, so that session knowledge survives host rebuilds.

#### Acceptance Criteria

1. WHEN the playbook completes THEN `~/.claude/projects/-home-ladino/memory` SHALL symlink to `~/code/claude-config/memory`
2. WHEN the playbook completes THEN `~/.claude/memory` SHALL symlink to `~/code/claude-config/memory`
3. IF the symlinks already exist correctly THEN the task SHALL NOT recreate them

### Requirement 5: Service Auto-Start

**User Story:** As a homelab operator, I want all user services to start on boot without login, so that cmd-center1 works after a power cycle.

#### Acceptance Criteria

1. WHEN the playbook runs THEN `loginctl enable-linger ladino` SHALL be executed (idempotent)
2. WHEN the spec-workflow-dashboard.service unit is installed THEN it SHALL be enabled at `default.target`
3. IF the host reboots THEN the dashboard SHALL be reachable within 60 seconds without login

### Requirement 6: Bootstrap Prereqs Documented

**User Story:** As a homelab operator, I want the manual bootstrap prereqs documented, so that I know exactly what steps to perform before running the playbook.

#### Acceptance Criteria

1. WHEN the README is read THEN it SHALL list the exact manual steps required before the first playbook run
2. IF a user follows the prereqs THEN they SHALL result in an Ansible-reachable host

## Non-Functional Requirements

### Recovery Time Objective (RTO)
- Total time from fresh Ubuntu install to fully working cmd-center1: under 30 minutes (excluding Ubuntu install time itself)

### Compatibility
- Target OS: Ubuntu 24.04 (Noble)
- Target user: ladino (non-root, with sudo via NOPASSWD for specific commands)
- Ansible runner: can be another Linux host with SSH access, or cmd-center1 running against itself

### Scope Exclusions
- SSH server hardening (handled by separate `common` role)
- VS Code Server or VS Code Remote SSH setup (handled manually by user)
- GPU or hardware-specific config (cmd-center1 is a simple VM)

## References
- Existing role: `roles/cmd_center/`
- Bootstrap script: `~/code/claude-config/bin/bootstrap.sh`
- Related: `roles/onepassword_cli/` (may already handle `op` install)
