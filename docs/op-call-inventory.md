# Direct `op` Call Inventory

Audit of every direct 1Password CLI invocation in this repo, tracked per spec `proxmox-inventory-vault` requirement 3.1. Updated 2026-05-02 as part of issue #124.

The point of this doc is to show that every `op` call either goes through `scripts/lib/op-secret-cache.sh` (which gates with a kill switch, caches values 48h, and serializes refreshes per slug with flock) or has been moved to ansible-vault. No call should bypass both.

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
| `scripts/vault-pass.sh` (`cached_op_read ansible_vault_password ...`) | CACHED | `op-secret-cache.sh` | Pulls the vault password itself. Cache TTL 48h, served stale under killswitch. |
| `scripts/sync-prometheus-targets.sh` | VAULT | `lib/proxmox-vault.sh` | Migrated from direct `op read` to vault decrypt as part of issue #124. |
| `scripts/lib/proxmox-vault.sh::load_proxmox_token_from_vault` | VAULT | (vault decrypt) | Replaces the direct `op read` for the dynamic inventory plugin. |
| `/usr/local/bin/op` attribution shim (Ansible role `op_ratelimit_collector`) | PASS-THROUGH | execs `/usr/bin/op` | Logs every invocation for attribution, adds no op call of its own. |
| `/usr/local/bin/op-quota-collector.sh` (Ansible role `op_ratelimit_collector`) | CONTROL-PLANE | direct `op service-account ratelimit` | Free per the docs and the 2026-04-19 verification. Confirmed 2026-05-02: collector ran every 5 min during the read_write cap exhaustion without changing the USED counter. |
| `roles/onepassword_cli/tasks/main.yml` (`op service-account ratelimit`) | CONTROL-PLANE | direct | Token verification. Was `op vault list` (billable) until 2026-09-13; the ratelimit call still authenticates (rc 1 with no token, rc 9 with a bad one) but costs nothing. |
| `scripts/run-cmd-center.sh` | CACHED | `op-secret-cache.sh` via `vault-pass.sh` | Operator-run wrapper for `cmd_center.yml`. Preloads nothing: the play reads no env secrets, only the vault password. |
| `/etc/profile.d/op-ansible-env.sh` (`roles/cmd_center/tasks/shell_env.yml`) | VAULT | `lib/proxmox-vault.sh` | Interactive bash only. Replaced an unmanaged file that ran an uncached `op read` of the Proxmox token at shell startup, see below. |

## Shell startup must never call op

Until 2026-09-13, `~/.bashrc` on command-center1 ran `op read "op://Infrastructure/Proxmox API/Ansible Inventory/token_secret"` above its non-interactive guard whenever `PROXMOX_TOKEN_SECRET` was unset, and an unmanaged `/etc/profile.d/op-ansible-env.sh` did the same for login and interactive shells. Ubuntu's bash sources `~/.bashrc` for commands run over SSH, so every SSH exec Ansible made into command-center1 paid for a read in the remote shell, invisible to the local process tree: measured 2 reads per `ssh command-center1 true` and 88 per `cmd_center.yml` run. `roles/cmd_center/tasks/shell_env.yml` now templates the profile file (vault-sourced, interactive only), strips the block from `~/.bashrc`, and fails the play if any `op read` is left there.

If a quota jump lines up with SSH activity into command-center1 rather than with a wrapper, check the startup files first.

## Attribution: who is calling op

On command-center1 every `op` invocation passes through an attribution shim, `/usr/local/bin/op` (source: `roles/op_ratelimit_collector/files/op-shim`), which sits ahead of the real `/usr/bin/op` on PATH. It appends one JSON line to `/var/log/op-shim/op-invocations.log` and then execs the real binary unchanged. It makes no 1Password call of its own, and a logging failure never fails the op call.

Each line records the time, pid, ppid, the systemd unit, the outermost `*.sh` ancestor (`consumer`), the immediate parent (`caller`), the ancestor chain, the parent command line (secret-looking words redacted), an allowlisted subcommand, the cache slug when the call came from `cached_op_read`, and any `op://` reference. Other arguments are never logged because they can carry secrets.

The `op-quota-collector` timer folds the log into `onepassword_op_invocations_total{consumer,caller,unit,subcommand,slug}` in `/var/lib/node_exporter/textfiles/op_invocations.prom` and rotates the log at 5 MiB.

Useful queries:

```promql
# Billable calls by consumer over the last day (ratelimit is free)
sum by (consumer, caller, slug) (increase(onepassword_op_invocations_total{subcommand!="service-account ratelimit"}[1d]))
```

To see a single spike in detail, read the log around its timestamp:

```bash
jq -c 'select(.ts >= 1789200000 and .ts < 1789200600)' /var/log/op-shim/op-invocations.log
```

Blind spots: the shim does not see External Secrets Operator in Kubernetes, op calls on other hosts, or anything that runs `/usr/bin/op` by absolute path. All of those draw on the same account quota. If a quota jump has no matching shim lines, the source is one of those.

## Forcing a cache refresh

After changing an item in 1Password, mark the affected slugs stale:

```bash
scripts/op-secret-refresh.sh --list                 # slugs and ages, never values
scripts/op-secret-refresh.sh grafana_pg_password    # one or more slugs
scripts/op-secret-refresh.sh --all                  # everything
```

This makes no op call. The next reader re-reads each slug once, and keeps serving the old value if that read fails. Start `ansible-proxmox.service` or `ansible-security.service` to pick the change up immediately.

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
