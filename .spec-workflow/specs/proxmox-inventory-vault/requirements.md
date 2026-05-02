# Requirements Document

## Introduction

Move the Proxmox API token from a runtime `op read` call inside the dynamic inventory plugin to an ansible-vault-encrypted variable consumed at play start. This closes the op-cache-bypass path documented in the 2026-04-19 incident where `vm_baseline.yml` burned ~500 1P reads per run (~48 reads/min sustained) before it was killed.

The root cause: `scripts/lib/op-secret-cache.sh` caches secrets for playbook BODY tasks via `lookup('env', ...)`, but the Proxmox dynamic inventory plugin loads before the env cache is on the path and calls `op read` directly. Each ansible fork re-resolves the inventory, multiplying calls across hosts.

The fix (option 2 from the memory): put the token in `group_vars/vault.yml`, decrypt at play start, export to env for tasks that need it. Proxmox API token rotation becomes a vault re-encrypt, not a 1P item fetch per run. Aligns with the pattern already used by `ansible-security.yml`.

This spec is a companion to the k8s-argocd `eso-rate-limit-hardening` spec. Both address the same class of problem (uncontrolled 1P reads) from different sides.

## Alignment with Product Vision

Prime directive: everything managed in code, no manual configuration. Ansible Vault is the canonical "secrets in code" answer for this repo. The current bypass forces a runtime dependency on 1Password for an operation that runs on a 5-minute timer, which is the worst possible cadence profile for a daily-capped API.

The op-killswitch (PR #104) and op-secret-cache (PR #105-#106) already implement most of the hygiene. This spec closes the single remaining unprotected path.

## Requirements

### Requirement 1: Token moves into ansible-vault

**User Story:** As an operator, I want the Proxmox API token to live in `group_vars/all/vault.yml`, encrypted with the repo's vault password, so that playbooks can read it without calling `op`.

#### Acceptance Criteria

1. WHEN the vault is decrypted THEN `group_vars/all/vault.yml` SHALL contain `vault_proxmox_api_token` (raw secret) and `vault_proxmox_api_token_id` (the token identifier, e.g. `root@pam!ansible`).
2. WHEN an unencrypted view of the repo is generated THEN `vault_proxmox_api_token` SHALL NOT appear anywhere in cleartext.
3. WHEN the token is rotated in Proxmox THEN updating the repo SHALL be a single `ansible-vault edit` command, with no 1Password round-trip required.
4. WHEN the 1P item for the Proxmox API token still exists THEN the repo SHALL document that it is a backup copy only, not the runtime source.

### Requirement 2: Dynamic inventory reads from env, not `op`

**User Story:** As the Proxmox dynamic inventory plugin, I need the API token available via environment variable at invocation time, so that I never call `op` and cannot accidentally re-introduce the drain.

#### Acceptance Criteria

1. WHEN `ansible-playbook` is invoked via a wrapper script (e.g. `scripts/run-proxmox.sh`) THEN the wrapper SHALL decrypt the vault once and export `PROXMOX_API_TOKEN` / `PROXMOX_API_TOKEN_ID` before invoking `ansible-playbook`.
2. WHEN the Proxmox inventory plugin configuration is loaded THEN it SHALL reference the env vars, not a 1P URI.
3. WHEN an ansible fork re-resolves inventory THEN it SHALL inherit the env vars from the parent wrapper process and SHALL NOT call `op`.
4. WHEN `grep -rn "op read" --include="*.yml" --include="*.yaml" --include="*.py"` is run across the repo THEN the Proxmox inventory path SHALL return zero hits.

### Requirement 3: Audit and close the remaining `op` direct-call paths

**User Story:** As an operator, I want confidence that the Proxmox inventory is the only remaining direct `op` caller that bypasses the env cache, so that shipping this spec actually lands the class-of-bug fix and not just one instance.

#### Acceptance Criteria

1. WHEN the repo is audited THEN every `op read`, `op item get`, `op item list`, and `op vault list` invocation SHALL be accounted for in a new `docs/op-call-inventory.md`.
2. WHEN the audit finds additional bypass paths THEN each one SHALL be either converted to env-var consumption or explicitly marked "out of scope" with justification in the doc.
3. WHEN the audit is complete THEN at least 95% of `op` vault-item calls in the repo SHALL be routed through `scripts/lib/op-secret-cache.sh` or ansible-vault.

### Requirement 4: Kill switch coverage

**User Story:** As an operator, I want the kill switch to still be respected even after the Proxmox token moves to vault, so that a 1P-drain event can pause other consumers without this path needing to care.

#### Acceptance Criteria

1. WHEN the kill switch file (`/var/lib/ansible-quasarlab/1p-killswitch`) is present THEN wrappers SHALL still honor it for any residual `op` calls (op-quota-collector etc.), per existing behavior.
2. WHEN the kill switch is present AND the Proxmox token is in vault THEN the Proxmox playbooks SHALL still run, because they no longer need 1P.
3. WHEN documenting the new posture THEN `memory/ansible-quasarlab/1password-rate-limit-care.md` (or its feedback equivalent) SHALL be updated to note that Proxmox playbooks are now kill-switch-independent.

### Requirement 5: Vault hygiene

**User Story:** As a reviewer, I want vault operations to be self-documenting and reversible, so that rotation and emergency re-keying are not a ritual known only to one person.

#### Acceptance Criteria

1. WHEN a new vault variable is added THEN `docs/vault.md` SHALL gain an entry describing what it is, who reads it, and how to rotate it.
2. WHEN the vault password is rotated THEN `scripts/rotate-vault-password.sh` (new if absent) SHALL automate the re-encrypt.
3. WHEN `ansible-vault view` is run by CI THEN it SHALL fail if the vault fails to decrypt with the CI-provided password, catching accidental double-encrypt or bad merges.

## Non-Functional Requirements

### Code Architecture and Modularity

- One vault file per environment (`group_vars/all/vault.yml` for shared, `group_vars/proxmox/vault.yml` if Proxmox-specific grows). Do not sprinkle vault files across roles.
- Wrapper scripts SHALL continue to source `scripts/lib/op-secret-cache.sh` and `scripts/lib/op-killswitch.sh` for everything else, unchanged by this spec.
- The dynamic inventory plugin file itself SHALL NOT be rewritten from scratch; only the auth block SHALL change to read from env.

### Performance

- Wrapper start-up overhead from decrypting vault SHALL be under 2 seconds.
- Steady-state Proxmox `op read` call rate SHALL drop to zero (down from the documented ~48 reads/min peak during a run).

### Security

- Vault password SHALL remain at `scripts/vault-pass.sh` (sourced from `~/.ansible-vault-pass` or equivalent) per existing convention.
- Env vars holding the token SHALL be scrubbed from shell history via wrapper-managed subshell (`env | grep -v PROXMOX_API_TOKEN`).
- The token SHALL have least-privilege Proxmox permissions (read-only for inventory, per the pattern already used).
- When this spec ships THEN the old 1P item SHALL be rotated and kept as a break-glass backup only.

### Reliability

- If vault decryption fails THEN the wrapper SHALL exit non-zero before invoking `ansible-playbook`, rather than silently falling back to `op` or prompting interactively.
- CI SHALL test at least one Proxmox playbook dry-run path to catch env-var naming mistakes.

### Usability

- The rotation runbook SHALL fit on one screen. Target: 5-step procedure (rotate in Proxmox, ansible-vault edit, commit, push, restart wrapper).
