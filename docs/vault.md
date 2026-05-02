# Ansible Vault Reference

Encrypted variables for ansible-quasarlab. Decryption uses `scripts/vault-pass.sh`, which fetches the vault password from 1Password (cached 12h on disk) and falls back to a local `.vault_pass` file.

## Variable inventory

| Variable | File | Used by | Purpose |
|---|---|---|---|
| `vault_proxmox_api_token` | `group_vars/all/vault.yml` | `scripts/lib/proxmox-vault.sh` (sourced by `run-proxmox.sh`, `run-security.sh`, `sync-prometheus-targets.sh`) | Proxmox API token secret for the dynamic inventory plugin (`ansible@pve!inventory`). Replaced the runtime `op read` lookup that drained 1P rate limits per fork (issue #124). |
| `vault_elasticsearch_password` | `group_vars/vault.yml` | (legacy, may be unused since the Wazuh password moved to op-secret-cache) | Old Elasticsearch password from the pre-Wazuh stack. Verify usage before removing. |
| `vault_pve_api_token_value` | `group_vars/proxmox/vault.yml` | (none, stale dead code) | Old token value, **does not match production**, kept until a follow-up cleanup PR removes it. |

The non-secret token identity (`user: ansible@pve`, `token_id: inventory`) lives in `inventory.proxmox.yml`. Only the secret value is encrypted.

## Rotation runbook (5 steps)

To rotate `vault_proxmox_api_token`:

1. **Mint a new token in Proxmox**
   ```
   pveum user token add ansible@pve inventory-new --privsep=0
   ```
   Copy the secret value shown (Proxmox displays it exactly once).

2. **Update vault**
   ```bash
   cd /home/ladino/code/ansible-quasarlab
   ansible-vault edit group_vars/all/vault.yml --vault-password-file scripts/vault-pass.sh
   ```
   Replace the `vault_proxmox_api_token:` value, save, exit.

3. **Verify decrypt + plugin path**
   ```bash
   bash -c '
   export REPO_DIR=$PWD
   source scripts/lib/proxmox-vault.sh
   load_proxmox_token_from_vault && ansible-inventory --list >/dev/null && echo OK
   '
   ```
   Must print `OK`. If it fails, the new token is wrong, the vault file is corrupt, or the API token was created with privsep=1 (separate ACL set).

4. **Commit and push**
   ```bash
   git add group_vars/all/vault.yml
   git commit -m "chore(vault): rotate vault_proxmox_api_token"
   git push
   ```
   The CI vault-decrypt check (`.github/workflows/`) confirms the file is well-formed before merge.

5. **Revoke the old token in Proxmox**
   ```
   pveum user token remove ansible@pve inventory
   pveum user token modify ansible@pve inventory-new --new-name inventory  # rename to canonical
   ```
   Or delete the new one and reuse the old name as a single op. Verify a scheduled `ansible-proxmox.service` run still works after revocation (any drift means the rotation did not propagate).

## Vault password rotation

Different from variable rotation. The vault password itself decrypts the `$ANSIBLE_VAULT;1.1;AES256` files. To rotate:

```bash
ansible-vault rekey group_vars/all/vault.yml group_vars/vault.yml group_vars/proxmox/vault.yml \
    --vault-password-file scripts/vault-pass.sh \
    --new-vault-password-file /tmp/new-pass
```

Then update the 1Password item `op://Infrastructure/Ansible Vault Password/password` with the new value, delete the local cache (`sudo rm /var/lib/ansible-quasarlab/secrets/ansible_vault_password`), and verify the next `vault-pass.sh` invocation pulls the new password successfully.

## Disaster recovery: rebuilding the control node

If `command-center1` is destroyed and you are bringing up a fresh control node, follow `docs/disaster-recovery.md`. Short version of the vault-relevant pieces:

- The vault password lives in 1Password at `op://Infrastructure/Ansible Vault Password/password`. **That 1P item is the single off-host secret you must protect.** If it is lost (and no offline backup), the encrypted vault files in this repo cannot be decrypted.
- A 1Password service-account token at `~/.config/op/service-account-token` lets the new control node retrieve the vault password automatically via `scripts/vault-pass.sh`.
- A `.vault_pass` file at the repo root is the manual fallback if the 1P CLI is unavailable. Mode 0600, gitignored, paste the password from the 1P web UI.

### Vault password backup recommendations

The vault password is the bottleneck for recovery. To reduce single-point-of-failure risk:

- **Primary**: 1Password `op://Infrastructure/Ansible Vault Password/password`. The 1Password account itself has its own emergency kit, keep the printed copy offline and physically secure.
- **Secondary**: a sealed-envelope copy stored offsite with the operator (paper, in a safe), updated whenever the vault password is rotated.
- **Tertiary**: an encrypted backup on offline media (USB stick in a safe) covering the latest snapshot of `~/.config/op/service-account-token` AND the vault password, so a fresh control node can reach 1P + decrypt the repo without web-UI interaction.

The repo itself is on GitHub, so the encrypted secret values survive even total loss of the homelab. Loss of the vault password is the only un-recoverable failure mode.

## Why not 1Password Connect or HashiCorp Vault?

Considered and rejected for this repo's scope (homelab, single operator):

- **1Password Connect**: adds a runtime service that itself counts against the same 1P rate limits we are trying to escape. Net negative for this use case.
- **HashiCorp Vault**: full secrets broker with mTLS clients, audit logs, and dynamic secrets. Overkill for one operator and three Proxmox hosts. Right answer if we ever go multi-tenant.
- **Mozilla SOPS + age**: per-key encryption with asymmetric keys. Reasonable upgrade path. The trade-off is added tooling (sops, age binaries plus a key-management workflow) for a homelab that does not yet need per-secret access control.

Ansible Vault stays the right fit: zero runtime services, zero rate limits, in-repo, deterministic, and aligned with the prime IaC directive (everything in code).
