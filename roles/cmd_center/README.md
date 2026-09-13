# cmd_center role

Provisions the command center host (the Ansible controller + Kubernetes
management + spec-workflow dashboard workstation). Applied by
`playbooks/cmd_center.yml`.

## What this role installs

| Area | Details |
|------|---------|
| Apt packages | curl, python3-pip, python3-kubernetes, python3-openshift, python3-yaml |
| Ansible collections | kubernetes.core |
| CLI tools | jq (apt), gh (cli.github.com apt repo), terraform (hashicorp apt repo, signing key pinned by fingerprint in `defaults/main.yml`), helm (pinned binary), yq (pinned binary) |
| Kubeconfig | Fetched from first k8s control plane, installed at `~/.kube/config` |
| Systemd timers (system) | `ansible-proxmox.timer`, `ansible-security.timer` with their service units. Both run `TimeoutStartSec` bounded, from the automation checkout. |
| Automation checkouts | `/var/lib/ansible-quasarlab/{repo,observability}`, cloned here and force-synced to `origin/main` / `origin/master` by the runners on every run. Never edit these by hand. |
| Systemd linger | Enabled for `ansible_user` so user services survive logout |
| Git repos | All lab repos cloned under `~/code/`. This is the **operator** tree: humans edit it, timers do not read it. |
| Claude Code | `claude-config/bin/bootstrap.sh` run to set up `~/.claude` symlinks |
| Node runtime | Standalone Node 22 at `~/.local/lib/nodejs/current/` (isolated from system apt node) |
| Spec-workflow dashboard | systemd user service on port 5000, bound to 0.0.0.0 for LAN reach |
| Herdr server | Pinned, checksum-verified herdr binary at `~/.local/bin/herdr`, symlinked from `/usr/local/bin/herdr` so `herdr --remote` finds it over a non-login SSH shell (whose PATH lacks `~/.local/bin`; without it the client silently falls back to a local session), plus the `herdr.service` systemd user unit, enabled (not started). Only when `cmd_center_herdr_enabled` is true, which only command-center1 sets. |
| Herdr health | `herdr-health-collector.timer` user timer writing `herdr.service` state to the node_exporter textfile `herdr.prom` every minute |

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

# Only the herdr binary, unit, and health timer (never restarts a running server)
ansible-playbook playbooks/cmd_center.yml --tags herdr
```

Available tags per task file:

- `hashicorp_apt` (also runs under `packages` and `cli_tools`, and always first)
- `packages`
- `cli_tools`
- `kubeconfig`
- `ansible_timers`
- `linger`
- `git_repos`
- `claude_bootstrap`
- `node`
- `spec_workflow`
- `ce_review_viewer`
- `herdr`

## Idempotency

Every task is designed to be re-runnable without side effects:

- apt modules use their native state tracking
- Git clones use `update: false` so local commits are never clobbered
- Binary installs (helm, yq, Node) use pinned versioned paths. Node uses `creates:`, yq uses `get_url` comparison, and helm gates its download, extract, and copy on one stat of the versioned binary, extracting into a root-only temp directory that is removed afterwards, so a reboot that clears `/tmp` or a `--check` run cannot break it
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
- `cmd_center_herdr_enabled` set to `true` in host_vars to install herdr, its `/usr/local/bin/herdr` link, its user unit, and its health timer (command-center1 only today). Setting it back to `false` removes the link if it still points at the managed binary.
- `cmd_center_herdr_version`, `cmd_center_herdr_sha256` bump together to upgrade herdr (see below)

## Herdr binary: pinning and upgrades

The role installs herdr when it is missing, and otherwise leaves it alone.
If the installed binary's SHA-256 does not match `cmd_center_herdr_sha256`,
the play **fails** instead of replacing it, because a running server with
live agent panes may be using it. That also catches an interactive
`herdr update` that was never recorded here.

Checksum source: herdr publishes a SHA-256 per asset in
`https://herdr.dev/latest.json` (the `sha256` map, which its own installer
verifies against). There is no separate checksums file on the GitHub
release. Because that manifest only ever describes the latest release, the
checksum is recorded in `defaults/main.yml` at pin time rather than fetched
at run time, so a later change on herdr.dev cannot alter what gets installed.

To upgrade:

1. Read the new `version` and `sha256.linux-x86_64` from
   `https://herdr.dev/latest.json`. Cross-check by downloading
   `https://github.com/herdrdev/herdr/releases/download/v<version>/herdr-linux-x86_64`
   and running `sha256sum` on it.
2. Bump `cmd_center_herdr_version` and `cmd_center_herdr_sha256` together in a PR.
3. After merge: `ansible-playbook playbooks/cmd_center.yml --tags herdr -e cmd_center_herdr_upgrade=true`.

The new file replaces the old one by atomic rename, so the running server
keeps executing the binary it already loaded. Moving the server onto the new
version is a separate, deliberate step. Per herdr's docs, compatible
(endpoint generation 1 or later) servers keep running across client
updates, so a restart is not required just to use the new client.

## Herdr health metrics

node_exporter's systemd collector only reads the system manager, so it
cannot see `herdr.service`. `herdr-health-collector.timer` (a user timer)
runs `/usr/local/bin/herdr-health-collector.sh`, which writes:

- `herdr_systemd_unit_state{name="herdr.service",state=...}`, same shape as `node_systemd_unit_state`
- `herdr_health_collector_success`, 0 when the user manager did not return a recognised state
- `herdr_health_collector_timestamp_seconds`, for staleness

If the user manager itself dies, the timer dies with it. That shows up as a
stale timestamp, and directly as `user@1000.service` in node_exporter's
systemd allowlist for command-center1.

Tests: `uv run --with pytest --with pyyaml --with ansible-core pytest roles/cmd_center/tests`.

Use pytest, not `python3 -m unittest discover`. Several test modules are
pytest-style functions and some drive real Ansible modules, so plain unittest
imports them as a single failed test and skips every case inside, while still
reporting the rest of the suite.

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
   - `systemctl --user is-enabled herdr` returns `enabled` (on command-center1)
   - `~/.local/bin/herdr --version` reports the pinned version (on command-center1)
   - `ssh command-center1 command -v herdr` returns `/usr/local/bin/herdr`, i.e. a non-login shell finds it (on command-center1)
   - `/var/lib/node_exporter/textfiles/herdr.prom` is fresh (on command-center1)
4. Reboot the host and re-verify item 3 to confirm services auto-start via linger. On command-center1, also confirm `systemctl --user is-active herdr` returns `active`.

On a live command center where herdr was already running on demand, the
role only enables the unit. Cut over once, at a quiet moment, since
stopping the server ends every agent pane:
`herdr server stop && systemctl --user start herdr`.

## Related spec

Full design rationale lives in
`.spec-workflow/specs/cmd-center-dr-provisioning/`:

- `requirements.md` what the playbook must achieve
- `design.md` why each architectural decision was made
- `tasks.md` tracked progress per phase
