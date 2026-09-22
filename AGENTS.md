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

Four separate pytest surfaces exist. Running one proves nothing about the others:

```
pytest tests/                                # 1Password quota gate, cache, killswitch, wrappers
pytest roles/cmd_center/tests                # 7 modules: apt, helm, herdr, changelog, systemd scope
pytest roles/op_ratelimit_collector/tests
pytest roles/unattended_upgrades/tests
```

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
syntax check is not deployment validation.

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
