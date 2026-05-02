# Direct `op` Call Inventory

Audit of every direct 1Password CLI invocation in this repo, tracked per spec `proxmox-inventory-vault` requirement 3.1. Updated 2026-05-02 as part of issue #124.

The point of this doc is to show that every `op` call either goes through `scripts/lib/op-secret-cache.sh` (which gates with a kill switch and caches values 12h) or has been moved to ansible-vault. No call should bypass both.

## Categories

- **CACHED**: routes through `scripts/lib/op-secret-cache.sh::cached_op_read`. Hits `op` only on cache miss, served stale during rate-limit incidents.
- **VAULT**: secret has been moved to ansible-vault, `op` is no longer in the path.
- **CONTROL-PLANE**: a free, non-rate-limited `op` call (verified 2026-04-19 against `op service-account ratelimit`, which is documented as not counting against either per-token or per-account quotas).
- **OUT-OF-SCOPE**: a one-off operator-run command, not part of any scheduled or automated path.

## Inventory

| Path | Type | Routes through | Notes |
|---|---|---|---|
| `scripts/run-proxmox.sh` (`load_cached_secrets`) | CACHED | `op-secret-cache.sh` | Authentik, Grafana, Claude Bridge passwords. Proxmox token removed from this list, see issue #124. |
| `scripts/run-security.sh` (`load_cached_secrets`) | CACHED | `op-secret-cache.sh` | Wazuh manager / API / indexer passwords. Proxmox token removed from this list. |
| `scripts/vault-pass.sh` (`cached_op_read ansible_vault_password ...`) | CACHED | `op-secret-cache.sh` | Pulls the vault password itself. Cache TTL 12h, served stale under killswitch. |
| `scripts/sync-prometheus-targets.sh` | VAULT | `lib/proxmox-vault.sh` | Migrated from direct `op read` to vault decrypt as part of issue #124. |
| `scripts/lib/proxmox-vault.sh::load_proxmox_token_from_vault` | VAULT | (vault decrypt) | Replaces the direct `op read` for the dynamic inventory plugin. |
| `/usr/local/bin/op-quota-collector.sh` (Ansible role `op_ratelimit_collector`) | CONTROL-PLANE | direct `op service-account ratelimit` | Free per the docs and the 2026-04-19 verification. Confirmed 2026-05-02: collector ran every 5 min during the read_write cap exhaustion without changing the USED counter. |
| `roles/onepassword_cli/tasks/main.yml:94` (`op vault list`) | OUT-OF-SCOPE | direct, manual | Part of the `onepassword_cli` provisioning role on the `feature/1password-vault` branch (not yet merged). One-time setup verification, not on any scheduled path. Will need its own audit when that branch lands. |

## Validation

After issue #124 ships, the following greps must remain clean:

```bash
# Any direct op-read of the Proxmox API token (must be ZERO hits, only docstrings allowed):
grep -rn "Proxmox API/Ansible Inventory" --include="*.sh" --include="*.yml" --include="*.yaml" --include="*.py" \
    --exclude-dir=.venv --exclude-dir=.git --exclude-dir=.spec-workflow .

# Any inventory plugin Python that calls op directly:
find . -name "*.py" -path "*/inventory*" -not -path "*/.venv/*" | xargs grep -l "op " 2>/dev/null
```

If any new direct `op` caller is added in the future:

1. Decide whether the secret should live in vault (preferred for low-rotation values) or stay in 1P (preferred for values that rotate often or need centralized audit).
2. If vault: add to `group_vars/all/vault.yml` and follow the rotation runbook in `docs/vault.md`.
3. If op-cached: route through `cached_op_read` or `load_cached_secrets`, never call `op` directly.
4. Update this inventory.

The 2026-04-19 incident (sustained ~48 reads/min, ~500 reads consumed in one playbook run) was caused by a single bypass path. The class-of-bug fix is keeping this inventory current and using the cache or vault as the only allowed call sites.
