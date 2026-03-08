# Operational Runbooks

## Ansible Automation

### Automated Timer
Both playbooks run on a 30-minute systemd timer on cmd_center1:

- **Service**: `ansible-proxmox.service`
- **Timer**: `ansible-proxmox.timer` (OnBootSec=5min, OnUnitActiveSec=30min)
- **Script**: `scripts/run-proxmox.sh` — pulls latest from git, runs `proxmox.yml` then `monitoring.yml`
- **Logs**: `/var/log/ansible-quasarlab/ansible-*.log` (last 50 retained)

```bash
# Check timer status
systemctl status ansible-proxmox.timer

# View latest log
ls -t /var/log/ansible-quasarlab/ansible-*.log | head -1 | xargs cat

# Trigger a manual run
systemctl start ansible-proxmox.service

# Watch it run
journalctl -u ansible-proxmox.service -f
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

### VM Inventory
**pve2 (192.168.1.11):**
| VMID | Name | Role |
|------|------|------|
| 105 | cmd_center1 | Ansible control node, kubectl access |
| 106 | palworld | Game server (usually stopped) |
| 109 | k8cluster2 | K8s node with GPU |

**pve (192.168.1.10):**
| VMID | Name | Role |
|------|------|------|
| 100 | npm | Nginx Proxy Manager |
| 102 | elastic | Elasticsearch |
| 103 | grafana | Grafana |
| 104 | timescaleDB | TimescaleDB |
| 108 | ad | Active Directory |
| 110 | k8cluster1 | K8s node |
| 111 | k8cluster3 | K8s node |
| 112 | nginx1 | NGINX LB |
| 113 | nginx2 | NGINX LB |
