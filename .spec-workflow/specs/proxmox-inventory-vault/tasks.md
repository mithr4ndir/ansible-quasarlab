# Tasks Document

- [ ] 1. Audit every direct `op` call in the repo
  - File: `docs/op-call-inventory.md` (new)
  - Run `grep -rn '\bop\b.*read\|\bop\b.*item get\|\bop\b.*list' --include="*.yml" --include="*.yaml" --include="*.py" --include="*.sh" .`
  - Run `find . -name "*.py" -path "*/inventory*" | xargs grep -l "op " 2>/dev/null` for inventory plugins specifically
  - For each hit, record: file, purpose, whether it goes through `op-secret-cache.sh`, whether it is bypassed
  - Purpose: Establish the full scope of `op` callers before changing the biggest one
  - _Leverage: grep_
  - _Requirements: 3.1_

- [ ] 2. Create the vault entries for the Proxmox API token
  - Files:
    - `group_vars/all/vault.yml` (update: add `vault_proxmox_api_token` and `vault_proxmox_api_token_id`; encrypt via `ansible-vault encrypt_string`)
    - `group_vars/all/main.yml` or equivalent (update: map `proxmox_api_token: "{{ vault_proxmox_api_token }}"`)
  - Use the write-capable 1P token to fetch the current value one final time, rotate it in Proxmox, put the new value in the vault.
  - Purpose: Put the secret in the right place before changing the consumer
  - _Leverage: existing vault entries (e.g. how `ansible-security.yml` secrets are handled)_
  - _Requirements: 1.1, 1.2, 1.3_

- [ ] 3. Update `scripts/run-proxmox.sh` to decrypt vault and export env vars
  - File: `scripts/run-proxmox.sh`
  - At the top of the wrapper (after `op-killswitch` and `op-secret-cache` sourcing, before `ansible-playbook` invocation), run `ansible-vault view group_vars/all/vault.yml` and parse out the two vars, export them.
  - Alternative: rely on ansible's built-in vault decryption and let the plugin read via ansible lookup rather than env; pick whichever is shorter and less error-prone. Decide in design step.
  - Purpose: Ensure env vars are present for the inventory plugin before fork
  - _Leverage: existing wrapper structure_
  - _Requirements: 2.1_

- [ ] 4. Point the dynamic inventory plugin at the env vars
  - File: `inventory/proxmox.yml` (plugin config)
  - Change the `token_secret` / `token_id` references from a 1P URI to `{{ lookup('env', 'PROXMOX_API_TOKEN') }}` / `{{ lookup('env', 'PROXMOX_API_TOKEN_ID') }}`
  - Also modify the python inventory plugin if it does its own `op` call: replace `op read ...` with `os.environ["PROXMOX_API_TOKEN"]`
  - Purpose: Stop the bypass
  - _Leverage: existing plugin auth block_
  - _Requirements: 2.2, 2.3, 2.4_

- [ ] 5. Validate no residual `op` calls during a Proxmox run
  - File: none (smoke test)
  - Run `./scripts/run-proxmox.sh playbooks/vm_baseline.yml --check --diff` with `strace -f -e trace=execve -o /tmp/strace.log ansible-playbook ...` attached to catch any `op` execs
  - Cross-check against `op service-account ratelimit` before and after to confirm zero `op` reads consumed
  - Purpose: Prove the drain is gone
  - _Leverage: the 2026-04-19 memory's measured rate as the baseline_
  - _Requirements: 2.4, NFR performance_

- [ ] 6. Close any other bypass path flagged by the audit
  - Files: per task 1's findings
  - For each unprotected `op` caller: either route through `op-secret-cache.sh`, move to vault, or explicitly mark out-of-scope in the inventory doc with reason
  - Purpose: Class-of-bug fix, not one-instance fix
  - _Leverage: task 1 inventory, existing cache and vault patterns_
  - _Requirements: 3.2, 3.3_

- [ ] 7. Update `docs/vault.md` and the op-call inventory with final state
  - Files:
    - `docs/vault.md` (update: document the two new vault vars, rotation procedure, 5-step runbook)
    - `docs/op-call-inventory.md` (update from task 1 to final state)
  - Purpose: Durable documentation
  - _Leverage: task 1 draft_
  - _Requirements: 5.1, NFR usability_

- [ ] 8. Verify kill-switch independence
  - File: none (smoke test)
  - Touch `/var/lib/ansible-quasarlab/1p-killswitch` on command-center1
  - Run `./scripts/run-proxmox.sh playbooks/vm_baseline.yml --check`
  - Confirm the playbook runs to completion (no `op` calls attempted, no failure)
  - Remove the kill switch after the test
  - Purpose: Prove Requirement 4.2
  - _Leverage: existing kill switch mechanism_
  - _Requirements: 4.1, 4.2_

- [ ] 9. CI addition: assert vault can be decrypted by CI
  - File: `.github/workflows/ci.yml` (or the canonical CI file)
  - Add a job that runs `ansible-vault view group_vars/all/vault.yml` with the CI vault password; fail the PR if it cannot decrypt.
  - Purpose: Catch merge-induced double-encrypt or bad-ref vault files before they land.
  - _Leverage: existing CI structure_
  - _Requirements: 5.3_

- [ ] 10. Update feedback memory after merge
  - File: `~/.claude/projects/-home-ladino/memory/ansible-quasarlab/2026-04-19_dynamic_inventory_op_cache_bypass.md` (mark resolved with PR link) and `MEMORY.md` (move from active issue list to resolved incidents)
  - Purpose: Keep the memory index honest
  - _Leverage: existing memory structure_
  - _Requirements: none directly (hygiene)_
