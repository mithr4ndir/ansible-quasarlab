# Operational Runbooks

## Ansible Automation

### Automated Timers

Two system-scoped timers on cmd_center1 enforce config on a schedule.

| | `ansible-proxmox` | `ansible-security` |
|---|---|---|
| Cadence | `OnUnitActiveSec=1h`, `OnBootSec=5min` | hourly |
| Script | `scripts/run-proxmox.sh` | `scripts/run-security.sh` |
| Playbooks | `proxmox`, `vm_baseline`, `monitoring`, `grafana_config`, `jellyfin`, `authentik`, `lb_setup`, `deploy-ha` | `wazuh`, `crowdsec` |
| Logs | `/var/log/ansible-quasarlab/ansible-*.log` (last 50) | `.../security-*.log` |

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

!!! danger "Only merged code is deployed"
    Because the runner pins to `origin/main`, anything unmerged is never
    applied, and anything applied from an unmerged branch is **reverted** on the
    next run. Merge before you expect a change to stick.

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

### Drive Layout (tank pool — 4 mirrors)
| Drive | Model | Mirror | Notes |
|-------|-------|--------|-------|
| sda | Inland SATA SSD 4TB | mirror-2 | SMART monitoring disabled |
| sdb | Inland SATA SSD 4TB | mirror-1 | SMART monitoring disabled |
| sdc | Inland SATA SSD 4TB | mirror-2 | SMART monitoring disabled |
| sdd | Inland SATA SSD 4TB | mirror-3 | SMART monitoring disabled |
| sde | Inland SATA SSD 4TB | mirror-3 | SMART monitoring disabled |
| sdf | WD Blue SA510 4TB | mirror-0 | SMART monitoring enabled |
| sdg | WD Blue SA510 4TB | mirror-0 | SMART monitoring enabled |
| sdh | WD Blue SA510 4TB | mirror-1 | SMART monitoring enabled |
| nvme0n1 | WD BLACK SN850X 4TB | SLOG | |
| nvme1n1 | WD BLACK SN850X 4TB | L2ARC | |
| nvme2n1 | YSR 128GB | boot-pool | |

**Why SMART is disabled on Inland SSDs:** These budget drives don't fully implement SMART log commands, causing TrueNAS to fire false-positive critical alerts. The drives are healthy — ZFS scrubs catch actual data errors regardless of SMART status.

**Why mirrors (RAID10) over RAIDZ:** Mixed workload — iSCSI block storage for K8s PVs and databases (TimescaleDB, Elastic) needs random I/O performance. Mirrors also resilver faster and allow expansion by adding pairs.

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

Note: as of 2026-08-24 no VM on pve2 has a `hostpci` entry. The GPU passthrough
described above is no longer attached to k8cluster2, consistent with the move
to CPU transcoding (`docs/decisions/2026-05-31-jellyfin-cpu-transcoding.md`).
Confirm with `grep -l hostpci /etc/pve/qemu-server/*.conf` before assuming
either way.

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
