# ansible-quasarlab — QuasarLab Configuration Management

Ansible playbooks and roles for configuring all VMs and bare-metal hosts in a Proxmox-based homelab. Uses dynamic inventory from the Proxmox API and secrets from 1Password via Ansible Vault.

## What's Managed

### Kubernetes Cluster (3 nodes)

| Host | IP | Role |
|------|----|------|
| k8cluster1 | 192.168.1.90 | Control-plane + worker |
| k8cluster2 | 192.168.1.89 | Control-plane + worker |
| k8cluster3 | 192.168.1.91 | Control-plane + worker |

**Roles:** OS prep (swap, sysctl, chrony), containerd, kubeadm/kubelet/kubectl, cluster init/join, CNI, MetalLB, ArgoCD

### Load Balancers (HA pair)

| Host | IP | Services |
|------|----|----------|
| nginx1 | 192.168.1.92 | HAProxy, Keepalived (VIP: 192.168.1.20) |
| nginx2 | 192.168.1.93 | HAProxy, Keepalived (staggered reboots) |

### Jellyfin Media Server (dedicated VM)

| Host | IP | Services |
|------|----|----------|
| jellyfin | 192.168.1.170 | Jellyfin (native), CPU transcoding, NFS media mount, Prometheus metrics |

Runs natively (not Docker) and transcodes in software on the VM's CPU.

GPU passthrough was removed on 2026-05-31: the RTX 2080 Ti repeatedly fell off
the bus under VFIO and the card was repurposed. The role now actively removes
the old `gpu-watchdog`, `gpu-metrics` and NVIDIA config rather than deploying
them. See `roles/jellyfin/README.md` and
`docs/decisions/2026-05-31-jellyfin-cpu-transcoding.md`.

### Wazuh SIEM (all-in-one)

| Host | IP | Services |
|------|----|----------|
| wazuh | 192.168.1.171 | Wazuh Manager + Indexer (OpenSearch) + Dashboard |

An agent is deployed on every managed Linux host. Check the live roster with
`agent_control -l` on the manager rather than trusting a number written here;
it was 15 plus the manager on 2026-09-19, and the count moves whenever a host
is added. Agent groups: `linux`, `kubernetes`,
`proxmox`, and `detlab` for the detection-lab hosts. Group membership comes from
`wazuh_agent_groups`, set per Ansible group in `group_vars/`. Note that
`group_vars/detlab.yml` **replaces** `linux` rather than adding to it, so
detonation alerts can be filtered out of real telemetry with
`NOT agent.group: detlab`.

### Proxmox Hosts

| Host | IP | Key Config |
|------|----|------------|
| pve | 192.168.1.10 | Primary node |
| pve2 | 192.168.1.11 | Hosts 2 of the 3 etcd members, so rebooting it takes the K8s control plane with it |

**Roles:** Networking, HA (qdevice on TrueNAS), iSCSI integration, hookscripts.

No VM has held a GPU since 2026-05-31. `pve_gpu_vms` is empty, which means the
hookscripts role tears the passthrough machinery down instead of deploying it;
see `roles/pve/hookscripts/README.md`. Before rebooting either node, read
"Rebooting a Proxmox node" in `docs/runbooks.md`, because the etcd colocation on
pve2 is a standing single point of failure.

### Baseline and monitoring (all Linux VMs)

`playbooks/vm_baseline.yml` is the single playbook that gives every Linux VM the
full stack. It targets `linux:!proxmox:!nas`, and it is what the hourly
`ansible-proxmox` timer runs:

- **Baseline** (`roles/common/vm_baseline`): qemu-guest-agent, chrony, sysctl tuning, capped journald, and the operator SSH keys. See `roles/common/README.md`.
- **node_exporter**: hardware/OS metrics (port 9100)
- **Vector**: log shipping to Loki via Vector Aggregator in K8s
- **Wazuh agent**: security monitoring
- **Unattended upgrades**: automated patching with per-host package blacklists

Proxmox hosts are excluded from that playbook (they are hypervisors, not VMs)
and are configured by `playbooks/proxmox.yml` and
`playbooks/proxmox-monitoring.yml` instead. They additionally get:

- **pve-exporter**: Proxmox API metrics (port 9221)
- **pve-quorum**: cluster quorum metrics via textfile collector

### Other VMs

| Host | IP | Purpose |
|------|----|---------|
| authentik | 192.168.1.50 | Authentik SSO (Docker), vm118 |
| command-center1 | 192.168.1.88 | Ansible controller, kubectl, ArgoCD CLI, 1Password CLI |
| musicbot | 192.168.1.157 | Discord music bot, vm107. See the note below |
| npm | 192.168.1.150 | Nginx Proxy Manager (reverse proxy) |
| postgresql | 192.168.1.123 | PostgreSQL, vm119 |
| timescaledb | 192.168.1.122 | TimescaleDB (Docker, port 5432) |
| TrueNAS | 192.168.1.15 | NAS (NFS, iSCSI), corosync qdevice |
| uptime-kuma | 192.168.1.129 | Uptime Kuma, the monitor outside the cluster: monitors as code via AutoKuma, NFS read probe, alerts straight to Discord. Static inventory; see `roles/uptime_kuma/README.md` |

This table is orientation, not an inventory. The authoritative per-node VM list,
verified against `qm list`, is "VM Inventory" in `docs/runbooks.md`, which also
covers the stopped templates.

`musicbot` was tagged `linux;services` on 2026-09-19 and is now managed. Until
then it carried no Proxmox tags at all, so it was in no group and no playbook
had ever touched it: no baseline, no node_exporter, no Wazuh agent, and no run
ever reported a problem. **The tag is not yet in `terraform-quasarlab`**, so a
rebuild from Terraform would drop it straight back out of the inventory. That is
the open half of issue #190.

There is no Grafana VM. Grafana moved into Kubernetes as a
`kube-prometheus-stack` subchart, its dashboards are ConfigMaps in
[k8s-argocd](https://github.com/mithr4ndir/k8s-argocd), and the `grafana_config`
role that used to deploy them has been removed from this repo.

## Inventory

Two sources, merged by `ansible.cfg`
(`inventory = inventory.proxmox.yml,inventory.static.ini`).

- **Dynamic:** `inventory.proxmox.yml`, auto-discovers VMs via the Proxmox API and groups them by their VM tags
- **Static:** `inventory.static.ini`, bare-metal/non-VM hosts (PVE nodes, TrueNAS), plus the untagged `uptime-kuma` VM, kept static so the out-of-cluster monitor can be deployed without the Proxmox API
- Proxmox API token sourced from `PROXMOX_TOKEN_SECRET` env var, decrypted from ansible-vault by `scripts/lib/proxmox-vault.sh` at wrapper start. See `docs/vault.md` for the rotation runbook and disaster-recovery bootstrap.

### How groups are formed

**A Proxmox VM tag becomes an Ansible group of the same name.** The
`keyed_groups` block in `inventory.proxmox.yml` turns every tag on a VM into a
group, so a VM tagged `linux;media` lands in both `linux` and `media`. Group
variables attach by that same name, which is why `group_vars/k8s.yml` and
`group_vars/detlab.yml` apply without anything listing their hosts. Most
playbooks target one of these tag-derived groups directly: `jellyfin.yml` runs
against `media`, `lb_setup.yml` against `lb`, `k8s_init.yml` against `k8s`.

The `linux` group is the important one, because the fleet-wide playbooks
(`vm_baseline.yml`, `monitoring.yml`, `wazuh.yml`, `patch-now.yml`) all target
it. It is built from two directions:

| Source | Mechanism |
|---|---|
| Tagged VMs | the `groups:` block in `inventory.proxmox.yml` puts a VM in `linux` when its tags contain `linux` |
| pve, pve2, truenas, uptime-kuma | `[linux:children]` in `inventory.static.ini`, which adopts the `proxmox`, `nas` and `uptime_kuma` groups wholesale |

**The failure direction is silent.** A VM with no Proxmox tags belongs to no
managed group, so no playbook targets it, and **nothing raises an error**. The
run succeeds, the recap lists only the hosts it did reach, and the untagged VM
is never mentioned. It is not unreachable and not failing, it is invisible.
musicbot (vm107) sat that way until 2026-09-19 with no baseline, no
node_exporter and no Wazuh agent, and no run ever said so (issue #190).

Tagging is therefore the step that makes a VM managed at all, not a labelling
convenience. See "Onboarding a new VM" in `docs/runbooks.md`.

To see what the inventory actually resolved to, rather than what you expect:

```bash
ansible-inventory --graph          # every group and its hosts
ansible-inventory --graph linux    # just the group the fleet playbooks target
```

Both need `PROXMOX_TOKEN_SECRET` in the environment; interactive shells on
command-center1 already have it. Compare the output against `qm list` on both
PVE nodes. A VM in `qm list` but in no group is an unmanaged host.

## Playbooks

```bash
ansible-playbook playbooks/vm_baseline.yml      # Baseline + monitoring, every Linux VM
ansible-playbook playbooks/jellyfin.yml         # Jellyfin VM only
ansible-playbook playbooks/monitoring.yml       # All monitoring agents
ansible-playbook playbooks/proxmox.yml          # PVE host config
ansible-playbook playbooks/proxmox-monitoring.yml  # PVE exporters + quorum
ansible-playbook playbooks/wazuh.yml            # Wazuh manager + agents
ansible-playbook playbooks/k8s_init.yml         # K8s cluster bootstrap
ansible-playbook playbooks/lb_setup.yml         # Load balancer pair
```

`site.yml` is **not** "run everything". It imports eight playbooks
(`cmd_center`, `lb_setup`, `k8s_init`, `k8s_final`, `monitoring`, `authentik`,
`proxmox`, `postgresql`) and notably does **not** include `vm_baseline`,
`jellyfin`, `wazuh`, `crowdsec`, `uptime-kuma` or `ups-shutdown`. Read it before
relying on it; the scheduled timers, not `site.yml`, are what actually keeps the
fleet converged. See "Automated Timers" in `docs/runbooks.md`.

For the command center itself, prefer the wrapper. It pins the automation checkout to
origin/main and adds the kill switch, quota pre-flight, vault-sourced Proxmox token and
a log, like the timers:

```bash
scripts/run-cmd-center.sh --check                 # defaults to --limit command-center1
scripts/run-cmd-center.sh --limit k8cluster1      # override the limit
```

`playbooks/uptime-kuma.yml` (vm117) has its own wrapper, which resolves the Discord #alerts
webhook from the 1Password cache and runs with `inventory.static.ini` alone:

```bash
scripts/run-uptime-kuma.sh --check
scripts/run-uptime-kuma.sh
```

`playbooks/ups-shutdown.yml` (NUT upsmon on the Proxmox hosts) has its own wrapper, which
resolves the upsmon password from the 1Password cache and works with 1Password down:

```bash
scripts/run-ups-shutdown.sh --check               # all proxmox hosts
scripts/run-ups-shutdown.sh --limit pve2          # one host
```

## Secrets

- **Ansible Vault** for encrypted variables (password from 1Password via `scripts/vault-pass.sh`). Includes the Proxmox API token (`vault_proxmox_api_token`).
- **1Password CLI cache** (`scripts/lib/op-secret-cache.sh`) for runtime-fetched secrets like Authentik, Grafana, Wazuh, Claude Bridge passwords. 48h TTL with per-slug locking, kill-switched against rate-limit drains. Every op call on command-center1 is attributed by a shim, see `docs/op-call-inventory.md`.
- **`scripts/lib/proxmox-vault.sh`** decrypts and exports `PROXMOX_TOKEN_SECRET` for dynamic inventory. Replaces the previous `op read` path that bypassed the env cache (issue #124). On command-center1, interactive bash shells get it from `/etc/profile.d/op-ansible-env.sh` (managed by `roles/cmd_center/tasks/shell_env.yml`), which uses the same lib and never calls `op`.
- See `docs/vault.md` for variable inventory, rotation runbook, and disaster-recovery bootstrap.
- See `docs/op-call-inventory.md` for the per-call-site audit of every `op` invocation in the repo.

## Related Repos

| Repository | Purpose |
|------------|---------|
| [k8s-argocd](https://github.com/mithr4ndir/k8s-argocd) | Kubernetes manifests, ArgoCD GitOps |
| [terraform-quasarlab](https://github.com/mithr4ndir/terraform-quasarlab) | VM provisioning on Proxmox |
| [observability-quasarlab](https://github.com/mithr4ndir/observability-quasarlab) | Grafana dashboards and provisioning |
