# Agent instructions: ansible-quasarlab

## Purpose

Ansible roles and playbooks that configure every host in the QuasarLab Proxmox homelab:
Kubernetes nodes, Proxmox hosts, the NAS, Jellyfin, Wazuh, load balancers, and
command-center1 itself. Changes here reach real machines on a timer, so treat a merge to
`main` as an operational action rather than a code change.

## Boundaries

- **The timers do not run from `~/code`.** Two systemd timers on command-center1 run from
  `/var/lib/ansible-quasarlab/repo`, force-synced to `origin/main` before every run
  (`scripts/lib/sync-repo.sh`), and abort the run if that sync fails.
  `ansible-proxmox.timer` fires hourly (`proxmox`, `vm_baseline`, `monitoring`,
  `jellyfin`, `authentik`, `lb_setup`, `deploy-ha`); `ansible-security.timer` every 30
  minutes (`wazuh`, `crowdsec`). A push to `main` deploys within the hour.
- Both directions of that matter: edits in `~/code` do not deploy until pushed, and
  diagnosing from `~/code` can be wrong because it drifts. On 2026-09-21 it was 30 commits
  stale. Use ARA for real run history.
- The timer units themselves are IaC, written by `roles/cmd_center` under the
  `ansible_timers` tag. Do not hand-edit them on the host.
- `inventory.proxmox.yml` is the dynamic Proxmox inventory. `inventory.static.ini` covers
  bare metal and the NAS and is safe to read.

## Validation

There is no CI in this repo. No `.github/workflows` exists, so every check below is one a
human or agent has to run deliberately.

Seven separate pytest surfaces exist. Running one proves nothing about the others:

```
pytest tests/                                # 1P quota gate, cache, killswitch, wrappers,
                                             # requirements drift, playbook syntax
pytest roles/cmd_center/tests                # apt, helm, herdr, changelog, systemd scope, task limits
pytest roles/common/vm_baseline/tests
pytest roles/op_ratelimit_collector/tests
pytest roles/unattended_upgrades/tests
pytest roles/k8s/common/tests
pytest roles/pve/gpu_passthrough/tests
pytest roles/uptime_kuma/tests
```

Adding a `tests/` directory to a role adds a surface nobody runs by habit. Either add it
to this list in the same commit, or put the test in `tests/` at the repo root.

Use the pinned runner, documented at `roles/cmd_center/README.md:151`:

```
uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" pytest roles/cmd_center/tests
```

The pin is load-bearing. Some tests drive Ansible's own conditional and templating
internals to evaluate `when:` and check mode exactly as production does, and those
internals move between releases: an unpinned install picked up 2.21.4, which had removed
`Conditional.evaluate_conditional`. Keep it matched to `ansible --version` on
command-center1.

Use pytest, never `python3 -m unittest discover`. Several modules are pytest-style
functions, and unittest imports them as one failed test while silently skipping every
case inside, so the run still looks mostly green.

For shell changes run `bash -n`. For role and template changes, review defaults and
templates and syntax-check against a static fixture inventory with fixture variables. A
syntax check is not deployment validation, but it is the only thing that resolves every
module name against the collections that actually exist, and
`tests/test_playbook_syntax.py` now runs it over all 21 playbooks. It pins the ini
inventory plugin and an empty callback directory, so it can never resolve the Proxmox
dynamic inventory or fire the 1Password quota gate. Never hand-run a syntax check with
the default inventory for the same reason.

## Community collections first

Do not reinvent what a collection already does. Before writing a `command`, `shell`, or
`script` task, check for a module. The control node has 103 collections and 8401 modules
installed already.

```
ansible-doc -l | grep -i <thing>     # modules available right now
ansible-galaxy collection list       # what is installed, and its version
ansible-doc <fqcn>                   # does this exact name exist
```

For something not installed yet, search Galaxy without a browser:

```
curl -s 'https://galaxy.ansible.com/api/v3/plugin/ansible/search/collection-versions/?keywords=proxmox&limit=10' \
  | jq -r '.data[].collection_version | .namespace + "." + .name + " " + .version'
```

Docs in an LLM-friendly form. `docs.ansible.com` answers 429 to this host, browser user
agent included, so use these `llms.txt` files or the `context7` MCP server. All verified
fetchable on 2026-09-27:

| what | llms.txt |
|---|---|
| Ansible docs (36k snippets) | https://context7.com/websites/ansible_projects_ansible/llms.txt |
| ansible-core docs source | https://context7.com/ansible/ansible-documentation/llms.txt |
| ansible.posix | https://context7.com/ansible-collections/ansible.posix/llms.txt |
| community.general | https://context7.com/ansible-collections/community.general/llms.txt |
| kubernetes.core | https://context7.com/ansible-collections/kubernetes.core/llms.txt |
| community.postgresql | https://context7.com/ansible-collections/community.postgresql/llms.txt |
| community.docker | https://context7.com/ansible-collections/community.docker/llms.txt |

Rules, each one earned:

- **A module over `command`/`shell`.** 69 `command`/`shell` tasks exist here. When you add
  another, the comment says why no module fits. What you give up is idempotence and check
  mode, so `changed_when` and `check_mode` are not optional on a raw task.
- **Declare what you use.** `requirements.yml` had three collections and needed five:
  `kubernetes.core` (16 tasks), `community.general` and `community.postgresql` were used
  and undeclared, and `community.grafana` was declared with nothing using it. Those runs
  worked only because the control node happened to have the collections from the
  `ansible` package, so a control node bootstrapped from `requirements.yml` alone, which
  is the documented DR path, would have failed. `tests/test_requirements.py` enforces both
  directions now.
- **Name the collection, not the core redirect.** `ansible.builtin.sysctl` works, because
  ansible-core keeps a redirect to `ansible.posix.sysctl`, and that is exactly how
  `ansible.posix` stayed an undeclared dependency. Write the real FQCN.
- **A well-formed FQCN is not a real module.** `community.general.dpkg_selections` does
  not exist. It passed lint, looked idiomatic, and made `playbooks/k8s_init.yml`
  unrunnable: `couldn't resolve module/action`. Check with `ansible-doc <fqcn>` before
  committing, and let the playbook syntax test catch the rest.
- **A third-party Galaxy role is a supply-chain dependency**, not a shortcut. Collections
  from the `ansible`, `ansible-collections`, `community` and `kubernetes` namespaces are
  the default; anything else gets the same scrutiny as any other pinned dependency, and
  a version floor that is proven on the control node rather than copied from a README.

## Landmines

- A green play recap is not an outcome. Verify the thing the play was supposed to change,
  not that the service is `active`. Wazuh was blind for four months while every unit
  reported healthy.
- `host_vars/<dir>` must match the inventory hostname exactly, including case. A mismatch
  is silently inert: `cmd_center1` and `timescaleDB` were both dead directories.
- node_exporter `--collector.systemd.unit-include` allowlists make alert rules vacuous for
  any unit not listed, and group lists override rather than merge. This hid three real
  outages. Confirm the series exists before writing a rule against it.
- Always set `TimeoutStartSec` on a timer-driven oneshot. Debian's apt units ship
  `TimeoutStartUSec=infinity`, and systemd will not re-trigger a timer whose oneshot is
  still `activating`, so one hung run ends all future runs with no alert. Both timers here
  set `TimeoutStartSec=3600` deliberately.
- `ANSIBLE_CALLBACK_PLUGINS` replaces `ansible.cfg`'s `callback_plugins` rather than
  adding to it, and ara sets it. An `AnsibleError` raised from a callback does not abort a
  play; raise `SystemExit`.
- Play-level `become` makes `ansible_user_uid` 0, so `/run/user/{{ uid }}` becomes
  `/run/user/0`. Use `become: false` for `scope: user` systemd tasks.

## Forbidden

- Do not run `ansible-playbook`, or anything that resolves `inventory.proxmox.yml`, to
  discover files or explore. Inventory resolution reaches 1Password and consumes a metered
  quota that has been exhausted four times.
- Do not run any `op` command that reads a secret. `op service-account ratelimit` is free
  and safe. `docs/op-call-inventory.md` audits every call site in the repo.
- Never print resolved secret variables.
- Do not modify `/var/lib/ansible-quasarlab/repo` directly, and do not run applying
  playbooks without explicit authority for that task.

## Completion

Changed behaviour, the files touched, the checks actually run, and what remains unverified.
For an authorised deployment, the intended outcome plus the play recap, not process state.
Do not commit unrelated changes; pushing `main` feeds the scheduled deployment.
