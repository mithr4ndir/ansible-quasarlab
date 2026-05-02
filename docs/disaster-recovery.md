# Disaster Recovery: Rebuild the Ansible Control Node

If `command-center1` is destroyed, this is the procedure to bring up a replacement and resume scheduled automation. The repo plus the 1Password vault is enough state to rebuild from zero.

## Prerequisites (off-host)

These artifacts must exist outside `command-center1` for recovery to be possible. Treat them as the "save game."

| Artifact | Where it lives | If lost |
|----------|----------------|---------|
| 1Password vault item: `Infrastructure/Ansible Vault Password` | 1Password cloud, Infrastructure vault | The encrypted vault files in this repo become unreadable. No automated recovery. Mitigation: keep an offline encrypted copy. |
| 1Password service account token (read-only is enough for runtime) | 1Password admin: Service Accounts. Issue a new one if needed. | Generate a new one, no recovery needed. |
| 1Password write-capable service account token | Same admin panel. | Same, generate new. |
| Proxmox API token (`ansible@pve!inventory`) | 1Password backup item AND ansible-vault (`vault_proxmox_api_token`). | If both are lost, generate a new one in PVE: `pveum user token add ansible@pve inventory --privsep 0`. Then update vault. |
| Repo: `git@github.com:mithr4ndir/ansible-quasarlab.git` | GitHub | If GitHub is gone, recover from a local clone on any host. |
| SSH private key for `ladino@` user across the fleet | Any controller, key is in `~/.ssh/id_ed25519` on each. | Re-issue: generate new keypair, push to all VMs via Proxmox console (one-time pain). |

## Step 1: provision a new control node

Use Terraform (`terraform-quasarlab`) to provision a new VM, then base-image it with cloud-init (Ubuntu LTS). Or build manually:

- **Hostname**: `command-center2` (avoid the original name to prevent stale DNS or SSH known_hosts confusion)
- **Network**: static IP in 192.168.1.0/24, route to 192.168.1.10/.11 (Proxmox)
- **OS**: Ubuntu 24.04 LTS (matches what the playbooks target)
- **User**: `ladino`, sudo NOPASSWD, your SSH public key in `~ladino/.ssh/authorized_keys`

## Step 2: install dependencies

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv git curl jq

# 1Password CLI
curl -sS https://downloads.1password.com/linux/keys/1password.asc \
  | sudo gpg --dearmor --output /usr/share/keyrings/1password-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/$(dpkg --print-architecture) stable main" \
  | sudo tee /etc/apt/sources.list.d/1password.list
sudo apt update
sudo apt install -y 1password-cli

# Ansible (in a venv to match the existing pinning)
cd /home/ladino
python3 -m venv .venv
source .venv/bin/activate
pip install ansible-core==2.18 community.proxmox PyYAML
```

## Step 3: clone the repo

```bash
mkdir -p /home/ladino/code
cd /home/ladino/code
git clone https://github.com/mithr4ndir/ansible-quasarlab.git
git clone https://github.com/mithr4ndir/observability-quasarlab.git
cd ansible-quasarlab
```

## Step 4: install secrets

This is the only step that needs human-in-the-loop access to 1Password. After this, the host is self-sufficient.

### 4a: 1Password service account token

```bash
mkdir -p ~/.config/op
chmod 0700 ~/.config/op

# Paste the service account token from 1Password admin:
# Service Accounts > <account name> > Reveal token
cat > ~/.config/op/service-account-token  # Ctrl+D after pasting
chmod 0600 ~/.config/op/service-account-token
```

### 4b: vault password

The vault password is in 1Password at `op://Infrastructure/Ansible Vault Password/password`. Once the service account token is in place, `scripts/vault-pass.sh` can fetch it via `op read`. To pre-populate the cache (avoiding a first-run `op` call):

```bash
sudo mkdir -p /var/lib/ansible-quasarlab/secrets
sudo chown ladino:ladino /var/lib/ansible-quasarlab/secrets
chmod 0700 /var/lib/ansible-quasarlab/secrets

# Either: let vault-pass.sh fetch it on first use (it will, automatically)
# Or: paste it from 1Password directly:
read -rs ANSIBLE_VAULT_PASSWORD
printf '%s' "$ANSIBLE_VAULT_PASSWORD" > /var/lib/ansible-quasarlab/secrets/ansible_vault_password
chmod 0600 /var/lib/ansible-quasarlab/secrets/ansible_vault_password
unset ANSIBLE_VAULT_PASSWORD
```

If 1Password is unavailable, fall back to a `.vault_pass` file at the repo root (gitignored):

```bash
read -rs ANSIBLE_VAULT_PASSWORD
printf '%s' "$ANSIBLE_VAULT_PASSWORD" > /home/ladino/code/ansible-quasarlab/.vault_pass
chmod 0600 /home/ladino/code/ansible-quasarlab/.vault_pass
unset ANSIBLE_VAULT_PASSWORD
```

`scripts/vault-pass.sh` checks 1Password (cached) first, then falls back to `.vault_pass`.

## Step 5: smoke test

```bash
cd /home/ladino/code/ansible-quasarlab

# Vault decrypt works?
ansible-vault view group_vars/all/vault.yml | head -3
# Expected: cleartext YAML with vault_proxmox_api_token

# Wrapper resolves inventory?
./scripts/run-proxmox.sh playbooks/vm_baseline.yml --check --diff --limit command-center2
# Expected: PLAY RECAP with ok=N, changed=0 (or low) failed=0 unreachable=0
```

## Step 6: install scheduled timers

The wrappers run on systemd timers managed by the `cmd_center` role. Apply it:

```bash
ansible-playbook playbooks/cmd_center.yml --diff --limit command-center2
```

This installs:
- `ansible-proxmox.service` and `.timer` (hourly)
- `ansible-security.service` and `.timer` (every 30 min)
- `op-quota-collector.service` and `.timer` (every 5 min)
- node_exporter, vector, wazuh-agent

## Step 7: enable timers

**Verify the cap is healthy first** (per `feedback_op_rate_limit_care.md`, scheduled timers can exhaust the daily 1P cap if dynamic inventory bypasses are not in place):

```bash
op service-account ratelimit
# Confirm account read_write REMAINING is well above 100 before enabling timers.
```

```bash
sudo systemctl enable --now ansible-proxmox.timer ansible-security.timer
```

## Step 8: decommission the old controller

If the old `command-center1` is recoverable but obsolete:

- Disable timers: `sudo systemctl disable --now ansible-proxmox.timer ansible-security.timer`
- Update DNS / Prometheus targets to point to the new host (per repo CLAUDE.md decommissioning checklist).
- Stop and destroy the VM in Proxmox.

## Out-of-scope

- **Rebuilding 1Password**. If the entire 1P account is lost, no recovery is possible from this repo alone. Keep a sealed-envelope offline copy of the vault password and at least one service account token if your threat model warrants it.
- **Rebuilding the Proxmox cluster itself**. Covered by the `proxmox-quasarlab` runbooks (out of repo, see `proxmox.md` persona memory).
