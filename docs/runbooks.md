# Operational Runbooks

## Ansible Automation

### Automated Timers

Two system-scoped timers on cmd_center1 enforce config on a schedule. They run
at different cadences; check the unit rather than assuming.

| | `ansible-proxmox` | `ansible-security` |
|---|---|---|
| Cadence | `OnUnitActiveSec=1h`, `OnBootSec=5min` | `OnUnitActiveSec=30min`, `OnBootSec=2min` |
| Script | `scripts/run-proxmox.sh` | `scripts/run-security.sh` |
| Playbooks | `proxmox`, `vm_baseline`, `monitoring`, `jellyfin`, `authentik`, `lb_setup`, `deploy-ha` | `wazuh`, `crowdsec` |
| Logs | `/var/log/ansible-quasarlab/ansible-*.log` (last 50) | `.../security-*.log` |

!!! note "Grafana is not managed here"
    Grafana runs in Kubernetes as a `kube-prometheus-stack` subchart. Its
    dashboards are ConfigMaps in
    [k8s-argocd](https://github.com/mithr4ndir/k8s-argocd/tree/main/infrastructure/monitoring/grafana-dashboards)
    loaded by the Grafana sidecar. The `grafana_config` role and playbook that
    used to deploy them were removed once Grafana left its VM.

### Where the timers run from, and why it matters

Timers execute from a **dedicated automation checkout**, not from anyone's
working tree:

```
/var/lib/ansible-quasarlab/repo           <- pinned to origin/main
/var/lib/ansible-quasarlab/observability  <- pinned to origin/master
```

At the start of every run each checkout is force-synced to its remote ref
(`scripts/lib/sync-repo.sh`), and the run **aborts** if that sync fails. A tree
the runner cannot verify is a tree it will not deploy from.

!!! danger "Only merged code is deployed. Reverting is not automatic."
    Because the runner pins to `origin/main`, unmerged work is never applied.
    Merge before you expect a change to stick.

    The reverse is weaker than it sounds. The next run re-applies whatever
    `main` **declares**, so a templated file that an unmerged branch changed
    does get reconciled back. Anything imperative or destructive does not:
    tasks absent from `main` have no inverse. From the 2026-08-24 accidental
    deploy, running `main` afterwards would have restored `encoding.xml`, but
    **not** reinstalled the purged NVIDIA packages, **not** recreated the
    deleted `/opt/jellyfin/config.migrated`, and obviously not un-restarted
    Jellyfin.

    After an accidental deploy, read the `changed:` lines in
    `/var/log/ansible-quasarlab/ansible-*.log` and remediate the irreversible
    parts by hand. Do not assume the next run cleaned up.

This replaced an older arrangement where the timers ran directly out of
`~/code/ansible-quasarlab` and tried to freshen it with
`git pull --ff-only origin main`. The return code was never checked and the
scripts do not use `set -e`, so when that tree sat on a feature branch the
fast-forward failed silently and the run applied **whatever was checked out**.
On 2026-08-24 that deployed an unmerged branch fleet-wide, Jellyfin restart
included. See ansible-quasarlab#144.

The practical upshot: you can leave any branch checked out under `~/code` on
cmd_center1 or a laptop without it reaching the fleet.

### Bootstrapping the automation checkout

The checkouts and the unit files are created by the `cmd_center` role, which is
**not** in either timer's playbook list, so this is a one-time manual step after
a rebuild or after changing the timer units:

```bash
ansible-playbook playbooks/cmd_center.yml --tags ansible_timers --diff
```

Verify:

```bash
git -C /var/lib/ansible-quasarlab/repo rev-parse --short HEAD   # == origin/main
systemctl cat ansible-proxmox.service | grep -E 'ExecStart|WorkingDirectory'
```

Both should point at `/var/lib/ansible-quasarlab/repo`. If `ExecStart` still
points into `~/code`, the bootstrap has not run and the fleet is being
configured from a mutable tree.

### Monitoring the timers

| Metric | Meaning |
|---|---|
| `ansible_run_success` / `ansible_playbook_success` | last run / per-playbook result |
| `ansible_run_timestamp_seconds` | drives `AnsibleRunStale` (>1h) |
| `ansible_run_repo_sync_success` | 0 when a run refused to start because it could not pin a checkout |
| `ansible_security_run_repo_sync_success` | same, for the security timer |
| `ansible_playbook_changed_tasks` | drives `AnsiblePlaybookMadeChanges` (info) |
| `ansible_playbook_last_run_info` | ARA report URL of the last run of each playbook |

`ansible_playbook_last_run_info` is what puts a clickable ARA link in the
Discord alert. Each wrapper run tags its playbooks with one ARA label,
`run:<wrapper>:<uuid>`, through the callback's `ARA_DEFAULT_LABELS`, then asks
the ARA API which playbook ids carry that label
(`scripts/lib/ara-run-links.sh`). Correlating on the label rather than "the
newest run of this playbook" is what keeps the link right when a manual run
overlaps a timer run. The lookup is time-bounded and fails open: if ARA is down
the series is simply absent that run, every other metric is written as usual,
and the alert carries no link rather than a generic one. The ARA address lives
in one place, `ARA_BASE_URL` in `scripts/lib/ara-run-links.sh`.

Failed runs are linked too, and they are the ones worth opening: the lookup
runs after the playbook loop, which never exits early on a non-zero
`ansible-playbook`. The one gap is a run that dies before its first play
starts (a syntax or inventory error), because the ARA callback attaches labels
at play start; such a run has an ARA record but no label, so it gets no link.

`AnsibleRepoSyncFailed` is **critical**: while it fires, no configuration is
being enforced anywhere. See the k8s-argocd `ansible-automation` rule group.

Both service units set `TimeoutStartSec=3600`. Do not remove it. systemd will
not re-trigger a timer whose oneshot is still `activating`, so an unbounded run
that hangs silently ends **all** future enforcement.

```bash
# Check timer status
systemctl status ansible-proxmox.timer
systemctl list-timers 'ansible-*'

# View latest log
ls -t /var/log/ansible-quasarlab/ansible-*.log | head -1 | xargs cat

# Trigger a manual run
systemctl start ansible-proxmox.service

# Watch it run
journalctl -u ansible-proxmox.service -f

# What is the automation checkout actually on?
git -C /var/lib/ansible-quasarlab/repo log --oneline -1
```

### Dry Runs
Always dry-run before adding new playbooks or roles to the timer:
```bash
ansible-playbook playbooks/<playbook>.yml --check --diff
```

**Known check-mode behaviors:**
- `get_url` reports `changed` but doesn't actually download files in check mode
- The node_exporter role handles this by skipping download/extract/install when the binary already exists (`stat` check)

### Running Playbooks Manually

Manual runs use **your** working tree, so they apply whatever you have checked
out. That is the point of keeping it separate from the automation checkout, but
it does mean a manual run can deploy uncommitted work. Never edit
`/var/lib/ansible-quasarlab/repo` by hand: the next run discards it.

```bash
cd /home/ladino/code/ansible-quasarlab

# Full run
ansible-playbook playbooks/proxmox.yml --diff
ansible-playbook playbooks/monitoring.yml --diff

# Target specific hosts
ansible-playbook playbooks/monitoring.yml --diff --limit k8cluster2

# Target specific roles
ansible-playbook playbooks/monitoring.yml --diff --tags node_exporter
```

### Onboarding a new VM

**Tag the VM first. Nothing else here works until you do.** Group membership is
derived from Proxmox VM tags, so an untagged VM is in no managed group, no
playbook targets it, and no run reports a problem. That is not a hypothetical:
musicbot (vm107) was built, ran in production, and was never once touched by
Ansible, because it carried no tags. No baseline, no node_exporter, no Wazuh
agent, and nothing anywhere said so (issue #190). See "How groups are formed" in
the repo README for the mechanism.

!!! danger "A missing tag is silent, not loud"
    Ansible reports on the hosts it reached. It cannot report on a host it was
    never told about. Treat "the run was green" as evidence about the hosts in
    the recap and about nothing else.

1. **Tag it in Proxmox.** Every managed VM needs `linux`. Add a role tag
   alongside it so group_vars apply: `linux;k8s`, `linux;media`, `linux;services`.

   ```bash
   qm set <vmid> --tags 'linux;services'
   qm config <vmid> | grep tags
   ```

2. **Codify the tag in Terraform.** The tag is infrastructure, not a one-off. A
   tag set only by hand is lost the next time the VM is rebuilt from
   `terraform-quasarlab`, which puts the host straight back into the
   unmanaged state above.

3. **Confirm it actually landed in the group.** Do not skip this; it is the
   check that would have caught #190.

   ```bash
   ansible-inventory --graph linux | grep <hostname>
   ```

   If the hostname is absent, stop and fix the tag. Nothing downstream will
   tell you.

4. **Add `host_vars/<hostname>/vars.yml` if the host needs overrides**, such as
   `unattended_upgrades_blacklist` for a managed service, or
   `node_exporter_systemd_units`. The directory name must match the inventory
   hostname **exactly, including case**; a mismatch is silently inert.

5. **Dry run, then apply.**

   ```bash
   ansible-playbook playbooks/vm_baseline.yml --check --diff --limit <hostname>
   ansible-playbook playbooks/vm_baseline.yml --diff --limit <hostname>
   ```

6. **Verify the host is genuinely managed**, not merely reachable:

   - the play recap shows the host with `failed=0` and `unreachable=0`
   - `ssh <hostname> 'systemctl is-active node_exporter chrony'` returns active
   - the Wazuh manager lists the agent as active, in the expected group
   - Prometheus has a target for the host and it is `up`
   - the operator SSH keys are present: see "Verifying a host actually got the
     key" in `roles/common/README.md`

7. **Audit the fleet for others like it.** Onboarding one host is a good moment
   to check that no other VM is hiding:

   ```bash
   # Every VM Proxmox knows about, on both nodes
   ssh root@192.168.1.10 qm list; ssh root@192.168.1.11 qm list
   # Every host Ansible knows about
   ansible-inventory --graph linux
   ```

   Anything in the first list and not the second is unmanaged. Stopped
   templates are expected; running VMs are not.

Removing a VM is the mirror image of this, and has its own rules about
Prometheus targets, inventory entries and alert routes. Follow the
decommissioning checklist in the repo `CLAUDE.md` rather than just deleting the
VM.

---

## TrueNAS SCALE

### Important: Appliance Constraints
TrueNAS SCALE is an appliance — some standard Linux operations don't work:
- `/usr` and `/opt` are **read-only** (ZFS boot pool)
- `apt` is **locked down** ("Package management tools are disabled on TrueNAS appliances")
- Ansible scope is limited to **monitoring agents only** (node_exporter)
- Networking, iSCSI, ZFS — all managed via TrueNAS UI/API, NOT Ansible

### ZFS Pool Health
```bash
ssh truenas_admin@192.168.1.15 'sudo zpool status tank -L'
```

| Status | Meaning |
|--------|---------|
| `ONLINE`, no errors | Healthy |
| `ONLINE` with CKSUM errors | Data corrected from mirror, investigate drive |
| `DEGRADED` | Drive failed, replace ASAP |
| `FAULTED` | Pool offline, data at risk |

### Clearing Stale Errors
Only do this after the underlying issue is resolved (e.g., drive reseated, cable replaced):
```bash
ssh truenas_admin@192.168.1.15 'sudo zpool clear tank'
```

### Drive Layout (tank pool, 4 mirrors)

`sd` letters are discovery order and can move across reboots. Treat this table
as the pool's shape, and resolve any individual drive by serial before acting
on it.

| Drive | Model | Mirror | Notes |
|-------|-------|--------|-------|
| sda | Inland SATA SSD 4TB | mirror-2 | SMART polling disabled, see below |
| sdb | Inland SATA SSD 4TB | mirror-1 | SMART polling disabled, see below |
| sdc | Inland SATA SSD 4TB | mirror-2 | SMART polling disabled, see below |
| sdd | Inland SATA SSD 4TB | mirror-3 | SMART polling disabled, see below |
| sde | Inland SATA SSD 4TB | mirror-3 | SMART polling disabled, see below |
| sdf | WD Blue SA510 4TB | mirror-0 | SMART monitoring enabled |
| sdg | WD Blue SA510 4TB | mirror-0 | SMART monitoring enabled |
| sdh | WD Blue SA510 4TB | mirror-1 | SMART monitoring enabled |
| nvme0n1 | WD BLACK SN850X 4TB | SLOG | |
| nvme1n1 | WD BLACK SN850X 4TB | L2ARC | |
| nvme2n1 | YSR 128GB | boot-pool | |

!!! warning "SMART is off on the Inland drives, and that is not a statement about their health"
    SMART polling is disabled on the five Inland SSDs because these budget
    drives do not fully implement the SMART log commands, so TrueNAS fires
    false-positive critical alerts when it polls them. **Disabling it was a
    workaround for a broken SMART implementation, not a finding that the drives
    are fine.** An earlier version of this page said "the drives are healthy".
    That was wrong, and it was wrong in the most expensive direction: it read
    as evidence when it was only an absence of evidence.

    Three of these drives are open RMA candidates for repeated bus dropouts,
    and two of them share a vdev. See below.

**The real failure mode is not in SMART.** These drives fail by dropping off the
SATA bus under sustained writes, which SMART would not report even if it worked
here: the drive stops answering, the link resets, and ZFS sees I/O errors. That
shows up in the kernel log, so that is where you look:

```bash
# On the NAS. Bus dropouts and link resets, which SMART will never show you.
dmesg | grep -ciE 'COMRESET|hard resetting|failed command'    # count
dmesg | grep -iE 'COMRESET|hard resetting|failed command|ata[0-9]+: ' | tail -40
zpool status tank -L
```

**Known bad batch (2026-09-16).** Three Inland IB24AK units, firmware/model
revision **VE1R9204**, in bays 2, 3 and 5, repeatedly drop off the bus under
sustained writes. Their **VE1R9004** siblings are clean, so this is a batch
problem, not a model problem. An RMA pack is prepared at
`~/rma-inland-ssd-2026-09-16.md`.

**mirror-3 pairs two of the bad-batch drives**, which is the worst available
placement: a single vdev whose both halves come from the same failing batch has
no healthy member to resilver from if the second one goes while the first is
being replaced. Rebalancing so each bad-batch drive is mirrored against a
known-good one is worth doing before any replacement, not after.

Both mirror-3 members, mapped by PARTUUID on 2026-09-19. PARTUUID and serial are
stable across reboots; `sd` letters are not, so identify these two by the
columns below and never by a letter:

| vdev member (PARTUUID) | Serial | Firmware |
|---|---|---|
| `701d7b32-63bb-4ae2-8501-73261bcff843` | IB24AK0004S00064 | VE1R9204 |
| `339fd304-fce6-45e9-8e63-bb0c164d8bfd` | IB24AK0004S00004 | VE1R9204 |

!!! note "Current state: quiet since 2026-09-19"
    Since the NAS reboot on 2026-09-19 the error count is **zero**
    (`dmesg | grep -ciE 'COMRESET|hard resetting|failed command'`). The drives
    are not throwing errors today. That is worth knowing so this page is not
    read as an active incident, and it is **not** a reason to close the RMA:
    the failures are load-dependent and the batch evidence has not changed.
    Re-run the count after any heavy write period, such as a large restore or a
    scrub.

**Why mirrors (RAID10) over RAIDZ:** mixed workload. iSCSI block storage for K8s
PVs and databases (TimescaleDB) needs random I/O performance. Mirrors also
resilver faster and allow expansion by adding pairs, which matters more than
usual while a bad batch is still in the pool.

### Identifying a physical drive before you pull it

**Do not identify a drive by its `sd` letter.** Linux assigns them in discovery
order, so they move across reboots, and this pool has been rebooted since the
drive layout table was written. Alphabetical order is not merely unstable here, it is
actively misleading: on this machine **`sdb` sits on `ata1` and `sda` on
`ata2`**.

On the DXP8800 chassis the bay LEDs map as `diskN` = kernel `ataN` = the **Nth
tray counting from the left** (confirmed visually 2026-09-16). That is the
mapping to trust, and it is not the `sd` ordering.

The authoritative chain, from a ZFS vdev member to a physical tray:

```bash
zpool status tank -L                          # PARTUUID per vdev member
readlink -f /dev/disk/by-partuuid/<uuid>      # -> /dev/sdX
readlink -f /sys/block/sdX                    # -> .../ataN/...   N is the tray, from the left
ls -l /dev/disk/by-id/ | grep -i inland       # serials, to cross-check against the RMA pack
```

!!! danger "On 2026-09-16 the wrong tray was pulled twice, and mirror-3 briefly had zero working disks"
    Bay 5 was lit. A helper pulled **bay 3** instead, twice. Bay 3 held the
    only live drive in mirror-3, whose other member was already down, so that
    vdev went to **zero working disks**. One more mistake there would have been
    data loss, not an inconvenience.

    It was caught only by reading `ata3: SATA link down` in the kernel log. The
    person's own account of which tray they had pulled was wrong **both times**,
    so do not accept a verbal report as evidence, including your own.

    After any reseat or pull, confirm which port actually changed before
    concluding anything:

    ```bash
    journalctl -k | grep -E 'ata[0-9]+: SATA link (up|down)|detaching|Attached SCSI disk'
    ```

### TrueNAS Alerts via API
```bash
API_KEY=$(cat ~/.config/truenas/api-key)

# List alerts
curl -sk "https://192.168.1.15/api/v2.0/alert/list" \
  -H "Authorization: Bearer ${API_KEY}" | \
  python3 -c "import json,sys; [print(f'{a[\"id\"]}  {a[\"level\"]}  {a[\"formatted\"][:80]}') for a in json.load(sys.stdin)]"

# Dismiss an alert
curl -sk "https://192.168.1.15/api/v2.0/alert/dismiss" \
  -X POST -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" -d '"<alert-id>"'

# Disable SMART on a drive
curl -sk "https://192.168.1.15/api/v2.0/disk/id/%7Bserial%7D<SERIAL>" \
  -X PUT -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" -d '{"togglesmart": false}'
```

### Config Backup
Daily automated backup via systemd timer on cmd_center1:
- **Timer**: `truenas-config-backup.timer` (OnCalendar=daily, RandomizedDelaySec=30min)
- **Script**: `/home/ladino/code/truenas-config-backup/backup.sh`
- **Repo**: Private GitHub repo (contains encrypted config DB with secretseed)
- **API key**: `~/.config/truenas/api-key` (chmod 600)

---

## Node Exporter

### Deployment
Managed via `roles/monitoring/node_exporter`. Deployed to all hosts in the `[linux]` group.

### TrueNAS-Specific
- Installs to `/var/lib/node_exporter` instead of `/usr/local/bin` (read-only filesystem)
- Configured via `host_vars/truenas/vars.yml`: `node_exporter_install_dir: /var/lib/node_exporter`
- Filebeat is skipped on NAS hosts (`when: "'nas' not in group_names"`)

### Idempotency
The role skips download/extract/install if the binary already exists at the configured path. To force a reinstall (e.g., version upgrade):
1. Update `node_exporter_version` in `roles/monitoring/node_exporter/defaults/main.yml`
2. Remove the old binary on the target host
3. Run the playbook

---

## Proxmox Hosts

### GPU Passthrough (pve2)

!!! warning "Historical as of 2026-05-31. Nothing below is live configuration."
    No VM on either node has held a GPU since 2026-05-31. The RTX 2080 Ti was
    removed after it repeatedly fell off the bus under VFIO, Jellyfin moved to
    CPU transcoding, and the card was repurposed. `pve_gpu_vms` is empty, so the
    `pve/hookscripts` role now tears this machinery down rather than deploying
    it, and `a3abfe7` purged the NVIDIA stack from the k8s nodes and the PVE
    hosts.

    Confirm before assuming either way:

    ```bash
    grep -l hostpci /etc/pve/qemu-server/*.conf    # expect: no matches
    ```

    See `docs/decisions/2026-05-31-jellyfin-cpu-transcoding.md` and
    `roles/pve/hookscripts/README.md`. Kept here because it is the record of
    what the configuration was, and what to restore if passthrough is ever
    reinstated. Note the two recovery layers had latent bugs that made them
    ineffective; the hookscripts README lists them, and they must be fixed
    before anyone relies on this again.

The configuration as it stood:

- RTX 2080 Ti passed to k8cluster2 (VM 109) via vfio-pci
- PCI addresses: `0a:00.0` (VGA), `0a:00.1` (Audio), `0a:00.2` (USB), `0a:00.3` (Serial)
- GRUB: `amd_iommu=on iommu=pt`
- VFIO IDs: `10de:1e07,10de:10f7,10de:1ad6,10de:1ad7`
- nvidia is blacklisted on the host — GPU is exclusively for VM passthrough
- VM 109 config: `machine: q35`, `hostpci0: 0000:0a:00.0,pcie=1,x-vga=0`

**GPU stability fixes applied:**
- **Driver**: 570.211.01 inside k8cluster2 (upgraded from 535 to fix RmInitAdapter failures)
- **GSP disabled**: `options nvidia NVreg_EnableGpuFirmware=0` in VM's `/etc/modprobe.d/nvidia.conf`
- **Hookscript**: `/var/lib/vz/snippets/gpu-reset.sh` — PCI remove/rescan before VM start
  - Attached: `qm set 109 --hookscript local:snippets/gpu-reset.sh`
  - Only triggers on `qm stop`/`qm start`, NOT on `sudo reboot` from inside VM
  - Logs: `/var/log/gpu-reset.log` on pve2

**Root cause**: NVIDIA GPUs retain internal state after faults (Xid 45 errors from ffmpeg/CUDA). Without a PCI bus reset, the GPU enters a dirty state where `RmInitAdapter` fails on the next driver load. The hookscript ensures a clean PCI reset on every VM start cycle.

### VM Inventory

Verified against `qm list` on both nodes 2026-08-24. The previous version of
this table was wrong in ways that matter for maintenance planning: it placed
k8cluster3 on pve (it is on pve2), and listed `elastic` and `grafana`, which
no longer exist as VMs.

**pve2 (192.168.1.11)** — 125G RAM, 32 cores:
| VMID | Name | Status | RAM | Role |
|------|------|--------|-----|------|
| 105 | command-center1 | running | 16G | Ansible control node, kubectl access |
| 106 | palworld | stopped | 32G | Game server |
| 109 | k8cluster2 | running | 16G | K8s control-plane + etcd member |
| 111 | k8cluster3 | running | 16G | K8s control-plane + etcd member |
| 115 | jellyfin | running | 12G | Media server (HA-managed, `vm:115`) |
| 116 | wazuh | running | 16G | SIEM manager + indexer |
| 118 | authentik | running | 4G | SSO |

**pve (192.168.1.10)** — 125G RAM, 48 cores:
| VMID | Name | Status | RAM | Role |
|------|------|--------|-----|------|
| 100 | npm | running | 6G | Nginx Proxy Manager |
| 104 | timescaleDB | running | 6G | TimescaleDB |
| 107 | musicbot | running | 4G | Discord music bot |
| 110 | k8cluster1 | running | 16G | K8s control-plane + etcd member |
| 112 | nginx1 | running | 4G | NGINX LB |
| 113 | nginx2 | running | 4G | NGINX LB |
| 117 | uptime-kuma | running | 2G | Uptime monitoring |
| 119 | postgresql | running | 4G | PostgreSQL |
| 101, 102, 108, 114, 9000 | templates / stopped | stopped | | Windows, AD, cloud-init templates |

Note: as of 2026-08-24 no VM on pve2 has a `hostpci` entry, consistent with the
2026-05-31 move to CPU transcoding. See "GPU Passthrough (pve2)" above, which is
marked historical for that reason.

### Rebooting a Proxmox node

A PVE host reboot is planned maintenance, never a quick fix for a
`RebootRequired` alert. Read this section before scheduling one.

#### The constraint that matters: etcd quorum

All three K8s nodes are control-plane members running etcd, and **two of the
three sit on pve2**:

| etcd member | host | etcd disk |
|---|---|---|
| k8cluster1 | pve  | local to pve |
| k8cluster2 | pve2 | `ssd_1:vm-109-disk-0` (20G, local to pve2) |
| k8cluster3 | pve2 | `ssd_2:vm-111-disk-0` (20G, local to pve2) |

etcd needs 2 of 3 to stay quorate. Rebooting pve2 takes out two members at
once, so the K8s control plane goes read-only and then unavailable, taking
Prometheus, Alertmanager, ArgoCD and External Secrets with it. Rebooting pve
takes out only one member and the cluster survives.

This is a standing single point of failure, not just a reboot inconvenience.
Spreading the etcd members across hosts is the real fix and is worth doing
independently of any reboot.

#### Storage: what can and cannot live-migrate

Root disks are on `truenas-iscsi`, which is shared, so those migrate live.
The etcd disks are on node-local lvmthin and do not:

- **Live-migratable**: 105 command-center1, 115 jellyfin, 116 wazuh, 118 authentik
- **Not cleanly live-migratable**: 109 k8cluster2, 111 k8cluster3 (local etcd disk,
  needs `qm migrate --with-local-disks`, which copies 20G and is offline for
  part of it)

Capacity is not the blocker: pve had 103G available against pve2's 83G in use
at the time of writing. Check both before starting.

#### Procedure for rebooting pve2

```bash
# 1. Confirm quorum is healthy BEFORE touching anything.
#    Expect "Quorate: Yes" and total votes 3 (2 nodes + QDevice).
pvecm status

# 2. Move the HA master off the node being rebooted.
#    pve2 is usually master; check and let it fail over.
ha-manager status

# 3. Migrate the shared-storage VMs to pve. These are live migrations.
for v in 105 116 118; do qm migrate $v pve --online; done

# 4. jellyfin (115) is HA-managed. Let HA relocate it rather than migrating
#    it by hand, so HA state stays consistent.
ha-manager migrate vm:115 pve

# 5. Deal with the etcd members. Pick ONE of:
#    (a) Migrate them too, accepting the local-disk copy:
#        qm migrate 109 pve --online --with-local-disks
#        qm migrate 111 pve --online --with-local-disks
#    (b) Accept K8s downtime: drain and shut them down, reboot, bring back.
#        Only acceptable in a window where losing the control plane is fine.
#    Option (a) is preferred. Verify etcd is healthy after EACH migration
#    before starting the next, and never move both at once.

# 6. Verify etcd is fully healthy and all 3 members are up.
kubectl get nodes
kubectl -n kube-system get pods | grep etcd

# 7. Reboot.
ssh root@192.168.1.11 reboot

# 8. After it comes back: confirm quorum, then confirm the new kernel.
pvecm status
uname -r

# 9. Migrate the VMs back and re-check quorum and HA.
ha-manager status
```

#### Post-reboot checklist

Per the decommissioning and maintenance rules, confirm after any node reboot:

- `pvecm status` shows Quorate with the QDevice contributing its vote
- all 3 etcd members are healthy, not just all 3 nodes `Ready`
- Prometheus targets for the node and its VMs are `up`
- no `NodeDown` or `ServiceInactive` alerts left firing in AlertManager
- `vector.service` is active on every K8s node (it does not always survive a
  Loki outage during boot; it exhausts its systemd restart budget and gives
  up, needing `systemctl reset-failed vector && systemctl start vector`)
