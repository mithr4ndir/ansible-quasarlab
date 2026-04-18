# Design: cmd-center1 DR Provisioning

## Overview

Extend the existing `roles/cmd_center/` role to cover all currently-manual provisioning steps. The new tasks are organized into topical task files included from `main.yml`, preserving the existing logic while adding new capabilities.

## Architectural Decisions

### Decision 1: Extend existing role, do not create a new role

The existing `cmd_center` role already targets the same host, owns kubectl config setup, and deploys the ansible-proxmox/security systemd timers. Creating a new role would require duplicate inventory entries, group_vars, and risk ordering bugs. Extending keeps related concerns co-located.

**Rejected alternative:** Split into `cmd_center_base` and `cmd_center_apps`. This adds indirection without clear ownership benefit for a single-host role.

### Decision 2: Standalone Node 22 binary at `~/.local/lib/nodejs/`

Ubuntu 24.04 ships Node 18.19.1 via apt. The spec-workflow-mcp dashboard requires Node 20+ (vite 8, tailwindcss/oxide, find-my-way all refuse Node 18). Installing Node 22 via NodeSource apt repo would replace the system Node, potentially breaking other apt packages that depend on node-18. A standalone tarball install in `~/.local/lib/nodejs/` isolates Node 22 to the user scope without touching system state.

**Rejected alternatives:**
- **nvm**: Adds another tool to the bootstrap chain, slower shell startup, harder to drive from systemd
- **nodesource apt repo**: Replaces system Node, broader blast radius
- **fnm**: Same objections as nvm

### Decision 3: systemd user service (not system) for spec-workflow

The dashboard runs as `ladino` and reads files under `~/.claude` and `~/code`. A system service would either require running as root (against security-first directive) or would need User= and StateDirectory= setup. A user service is simpler and matches "runs as the login user" semantics. Requires `loginctl enable-linger ladino` so it survives logout and starts at boot.

**Rejected alternative:** system service with `User=ladino`. Works, but requires careful tmpfiles.d config for writable paths, and does not inherit user env.

### Decision 4: 1Password service account token via ansible-vault

The `op` CLI needs a service account token at `~/.op_service_account_token` to unlock 1P and fetch SSH keys, kubeconfig, etc. This is the bootstrap secret that all other secret retrievals depend on. Storing it in ansible-vault keeps the playbook self-contained: operator runs `ansible-playbook --ask-vault-pass` and the token is decrypted and written to disk.

**Rejected alternative:** Operator manually creates the token file. Works but breaks the "single playbook run" goal.

### Decision 5: Git SSH over HTTPS

All git clones use `git@github.com:...` URLs. The SSH key deployed from 1P provides both authentication (clone) and push (for writing back memory updates). Using HTTPS would require either a PAT in 1P or leaving the repo read-only.

### Decision 6: Clone order with claude-config first

`claude-config` must be cloned before any other repo because:
1. `bin/bootstrap.sh` creates the `~/.claude/` symlinks (memory, settings, etc.)
2. The memory symlinks must exist before Claude Code sessions can write to them
3. Lab repos' `.mcp.json` files may reference paths that depend on `~/.claude/` setup

## Task File Structure

```
roles/cmd_center/
├── tasks/
│   ├── main.yml              # orchestrator: include_tasks in order
│   ├── packages.yml          # apt deps (extracted from current main.yml)
│   ├── cli_tools.yml         # NEW: helm, yq, gh, terraform (op handled by onepassword_cli role)
│   ├── onepassword_token.yml # NEW: write the 1P service account token from vault
│   ├── ssh_keys.yml          # NEW: fetch SSH key from 1P via op, deploy to ~/.ssh
│   ├── linger.yml            # NEW: enable systemd linger for ladino
│   ├── git_repos.yml         # NEW: clone claude-config + lab repos
│   ├── claude_bootstrap.yml  # NEW: run claude-config/bin/bootstrap.sh
│   ├── node_runtime.yml      # NEW: Node 22 standalone install
│   ├── spec_workflow.yml     # NEW: systemd user unit for dashboard
│   ├── kubeconfig.yml        # existing kubectl fetch (extracted)
│   └── ansible_timers.yml    # existing proxmox/security timers (extracted)
├── templates/
│   ├── spec-workflow-dashboard.service.j2    # NEW
│   ├── ansible-proxmox.service.j2            # existing
│   ├── ansible-proxmox.timer.j2              # existing
│   ├── ansible-security.service.j2           # existing
│   └── ansible-security.timer.j2             # existing
├── handlers/main.yml         # existing
└── defaults/main.yml         # extend with new variables
```

## Orchestration Order

The `main.yml` orchestrator must execute task files in strict dependency order:

```yaml
- include_tasks: packages.yml          # apt deps for everything below
- include_tasks: cli_tools.yml         # helm, yq, gh (some use apt)
- include_tasks: onepassword_token.yml # the bootstrap secret
- include_tasks: ssh_keys.yml          # depends on op token
- include_tasks: linger.yml            # independent
- include_tasks: git_repos.yml         # depends on SSH keys
- include_tasks: claude_bootstrap.yml  # depends on claude-config repo cloned
- include_tasks: node_runtime.yml      # independent
- include_tasks: spec_workflow.yml     # depends on node + linger
- include_tasks: kubeconfig.yml        # existing, depends on k8s nodes being up
- include_tasks: ansible_timers.yml    # existing, depends on repo cloned
```

## Key Variables

`defaults/main.yml` additions:

```yaml
node_version: "22.16.0"
node_install_dir: "/home/{{ ansible_user }}/.local/lib/nodejs"
lab_repos:
  - { name: claude-config,              url: "git@github.com:mithr4ndir/claude-config.git" }
  - { name: k8s-argocd,                 url: "git@github.com:mithr4ndir/k8s-argocd.git" }
  - { name: ansible-quasarlab,          url: "git@github.com:mithr4ndir/ansible-quasarlab.git" }
  - { name: observability-quasarlab,    url: "git@github.com:mithr4ndir/observability-quasarlab.git" }
  - { name: terraform-quasarlab,        url: "git@github.com:mithr4ndir/terraform-quasarlab.git" }
  - { name: quasarlab-disaster-recovery, url: "git@github.com:mithr4ndir/quasarlab-disaster-recovery.git" }
  - { name: truenas-config-backup,      url: "git@github.com:mithr4ndir/truenas-config-backup.git" }
spec_workflow_port: 5000
spec_workflow_bind_address: "0.0.0.0"
spec_workflow_cors_enabled: false
```

## Idempotency Design

| Task | Idempotency mechanism |
|------|----------------------|
| apt install | Ansible `apt:` module native |
| Node 22 download | `creates: {{ node_install_dir }}/node-v{{ node_version }}-linux-x64/bin/node` |
| Node 22 symlink | `file: state=link force=yes` |
| Git clone | `git: update=no` (does not force-pull) |
| Claude bootstrap | Script itself is idempotent (checks before creating symlinks) |
| Systemd unit deploy | Template with handler only on change |
| Linger | Check `/var/lib/systemd/linger/{{ ansible_user }}` before running |
| SSH key deployment | `no_log: true`, compare checksum before write |

## Security Considerations

1. **1P token file permissions**: `0600`, owner `ladino`, group `ladino`
2. **SSH private keys**: same as above
3. **`no_log: true`** on any task that handles secret content
4. **Ansible vault**: use `--ask-vault-pass` or vault password file (in 1P, manually retrieved)
5. **Service account token scope**: the 1P service account should have read-only access to the Infrastructure vault, not the master

## Verification (post-run)

```yaml
# tasks/verify.yml (optional final step)
- assert: that = "{{ lookup('url', 'http://127.0.0.1:5000', timeout=10) is defined }}"
- command: kubectl get nodes
- command: test -L "{{ ansible_user_dir }}/.claude/memory"
- command: test -L "{{ ansible_user_dir }}/.claude/projects/-home-ladino/memory"
```

## Open Questions

1. Should the playbook also install VS Code Server (for Remote SSH) or is that operator-initiated?
2. Should `cmd-center1` host its own sync-to-claude-config cron, or is that covered by the bootstrap symlink approach?
3. Do we need to support multiple command center hosts in the future (e.g., a laptop running the same setup)?
