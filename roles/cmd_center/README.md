# cmd_center role

Provisions the command center host (the Ansible controller + Kubernetes
management + spec-workflow dashboard workstation). Applied by
`playbooks/cmd_center.yml`.

## What this role installs

| Area | Details |
|------|---------|
| Apt packages | curl, python3-pip, python3-kubernetes, python3-openshift, python3-yaml |
| Ansible collections | kubernetes.core |
| CLI tools | jq (apt), gh (cli.github.com apt repo), terraform (hashicorp apt repo), helm (pinned binary), yq (pinned binary) |
| Kubeconfig | Fetched from first k8s control plane, installed at `~/.kube/config` |
| Systemd timers (system) | `ansible-proxmox.timer`, `ansible-security.timer` with their service units |
| Systemd linger | Enabled for `ansible_user` so user services survive logout |
| Git repos | All lab repos cloned under `~/code/` |
| Claude Code | `claude-config/bin/bootstrap.sh` run to set up `~/.claude` symlinks |
| Node runtime | Standalone Node 22 at `~/.local/lib/nodejs/current/` (isolated from system apt node) |
| Spec-workflow dashboard | systemd user service on port 5000, bound to 0.0.0.0 for LAN reach |

`op` (1Password CLI) is installed by the separate `onepassword_cli` role,
which is already listed in `playbooks/cmd_center.yml`.

## Manual prereqs before first run

1. Fresh Ubuntu 24.04 host with network access
2. User `ladino` with passwordless sudo (adjust `ansible_user` in inventory if using a different account)
3. SSH key on the Ansible runner that can reach the new host
4. Ansible vault password (see Vault section below)
5. 1Password service account token for the Infrastructure vault (consumed by the planned `onepassword_token.yml` task)

## Running

```bash
ansible-playbook -i inventory.static.ini playbooks/cmd_center.yml --ask-vault-pass
```

Selective runs with tags:

```bash
# Only reinstall CLI tools
ansible-playbook playbooks/cmd_center.yml --tags cli_tools

# Only redeploy the dashboard unit and restart it
ansible-playbook playbooks/cmd_center.yml --tags spec_workflow

# Only refresh the kubeconfig
ansible-playbook playbooks/cmd_center.yml --tags kubeconfig
```

Available tags per task file:

- `packages`
- `cli_tools`
- `kubeconfig`
- `ansible_timers`
- `linger`
- `git_repos`
- `claude_bootstrap`
- `node`
- `spec_workflow`

## Idempotency

Every task is designed to be re-runnable without side effects:

- apt modules use their native state tracking
- Git clones use `update: false` so local commits are never clobbered
- Binary installs (helm, yq, Node) use pinned versioned paths with `creates:` or `get_url` checksum comparison
- Symlinks use `force: true` to repoint without leaving duplicates
- systemd linger uses a stat check on the marker file
- `claude-config/bin/bootstrap.sh` is idempotent by design (checks for symlinks before writing)

## Vault

Secrets for this role live in `group_vars/cmd_center/vault.yml` (ansible-vault encrypted).
The vault password file path is set in `ansible.cfg` under `vault_password_file`.

Variables currently expected in the vault (once `onepassword_token.yml` lands):

- `op_service_account_token` read-only token for the Infrastructure vault

## Variables (defaults)

See `defaults/main.yml` for the full list. Key ones to override in inventory:

- `helm_version`, `yq_version`, `node_version` bump when upstream releases a new pinnable version
- `spec_workflow_bind_address` set to `127.0.0.1` for localhost-only
- `spec_workflow_cors_enabled` set to `true` and configure allowed origins if exposing beyond LAN
- `lab_repos` add or remove repos cloned onto the host

## Disaster recovery runbook

Target RTO: under 30 minutes from fresh Ubuntu 24.04 install to fully working
command center.

1. Fresh Ubuntu 24.04 install, user `ladino` with sudo, SSH accessible
2. On the Ansible runner: `ansible-playbook -i inventory.static.ini playbooks/cmd_center.yml --ask-vault-pass`
3. Verify:
   - `curl http://<host>:5000` returns HTTP 200 (dashboard)
   - `kubectl get nodes` works (kubeconfig installed)
   - `ls -la ~/.claude/memory` shows symlink to `~/code/claude-config/memory`
   - `systemctl list-timers` shows both ansible-proxmox and ansible-security timers
4. Reboot the host and re-verify item 3 to confirm services auto-start via linger

## Related spec

Full design rationale lives in
`.spec-workflow/specs/cmd-center-dr-provisioning/`:

- `requirements.md` what the playbook must achieve
- `design.md` why each architectural decision was made
- `tasks.md` tracked progress per phase
