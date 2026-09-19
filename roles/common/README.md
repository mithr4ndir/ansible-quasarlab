# common roles

Shared building blocks that other playbooks compose. Three sub-roles, applied by
role path rather than as `common` itself:

| Sub-role | Applied by | What it does |
|---|---|---|
| `common/vm_baseline` | `playbooks/vm_baseline.yml` (`linux:!proxmox:!nas`) | Baseline packages, qemu-guest-agent, chrony, sysctl tuning, capped journald, operator SSH keys |
| `common/apps/kubectl` | `playbooks/cmd_center.yml`, `playbooks/k8s_init.yml` | Installs and holds `kubectl` at the pinned `kube_version` |
| `common/os/debian` | not currently in any playbook | Debian package, networking and firewall tasks |

## vm_baseline: operator SSH keys

`vm_baseline` is what puts your SSH public keys on every managed VM, through
`ansible.posix.authorized_key`. Keys are **added, never removed**
(`vm_baseline_authorized_keys_exclusive` is false and should stay false).

Before changing anything here, read the two comment blocks that explain why this
exists and how it breaks. They are not repeated in this file, because the place
to read them is the place you are editing:

- `defaults/main.yml`, above `vm_baseline_authorized_keys`: why cloud-init is not enough, and why a host built before a key existed never receives it.
- `tasks/main.yml`, above the `authorized_key` task: why the `key:` scalar must be **double** quoted.
- `tests/test_authorized_keys.py` at the repo root, `test_no_key_has_a_mangled_comment`: why you must never copy a key line off a running host.

### Adding a new operator key

1. Append the **public** key line to `vm_baseline_authorized_keys` in
   `defaults/main.yml`. Take it from the `.pub` file, never from a host's
   `authorized_keys`.
2. Confirm the line is exactly three fields (type, blob, comment):

   ```bash
   awk '{print NF}' ~/.ssh/id_ed25519.pub     # expect: 3
   ```

3. Run the tests. They render the template through Ansible rather than reading
   its source, so they catch a mangled key or a broken join that eyeballing
   will not:

   ```bash
   uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" \
     pytest tests/test_authorized_keys.py
   ```

4. Dry run against one host and read the diff before merging:

   ```bash
   ansible-playbook playbooks/vm_baseline.yml --check --diff --limit <host>
   ```

   The diff must show **one key per line**. A single line containing `\n` or two
   `ssh-` prefixes means the join is broken and the second key authorises
   nobody.

5. Merge. The hourly `ansible-proxmox` timer runs `vm_baseline` from the pinned
   automation checkout, so unmerged work never reaches the fleet. See "Where the
   timers run from" in `docs/runbooks.md`.

### Removing a key

Deleting the line is not enough: `exclusive` is false, so the key stays on every
host that already has it. Removal is a deliberate, separate operation. Add the
key with `state: absent` in a one-off task, or remove it by hand, and confirm
per host. Do not flip `vm_baseline_authorized_keys_exclusive` to true to force
it: that deletes every key added out of band, including cloud-init's, and can
lock everyone out of a host.

### Verifying a host actually got the key

`ssh-keygen -l` reports the same fingerprint for a mangled line as a clean one,
so check the file shape as well as the fingerprint:

```bash
# On the target host. Expect one key per line, 3 fields each, and no `1~`.
awk '{print NR": "NF" fields"}' ~/.ssh/authorized_keys
grep -c '1~' ~/.ssh/authorized_keys          # expect: 0
ssh-keygen -l -f ~/.ssh/authorized_keys
```

A host that is missing a key entirely is usually a host no playbook reaches at
all. Check its Proxmox tags before debugging the role: see "How groups are
formed" in the repo README.

## Tests

```bash
uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" \
  pytest tests/test_authorized_keys.py roles/common/apps/kubectl/tests
```

Keep `ansible-core` pinned to what command-center1 runs. `test_authorized_keys`
drives Ansible's own templating to render the key list exactly as production
would, and those internals change between releases.
