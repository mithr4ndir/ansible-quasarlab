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
| jellyfin | 192.168.1.170 | Jellyfin (native), NVIDIA 570 drivers, GPU watchdog, NFS media mount |

Runs natively (not Docker) with RTX 2080 Ti GPU passthrough from pve2 for NVENC hardware transcoding. Includes GPU crash recovery watchdog and Prometheus metrics exporters.

### Wazuh SIEM (all-in-one)

| Host | IP | Services |
|------|----|----------|
| wazuh | 192.168.1.171 | Wazuh Manager + Indexer (OpenSearch) + Dashboard |

13 agents deployed across all Linux hosts. Groups: `linux`, `kubernetes`, `proxmox`.

### Proxmox Hosts

| Host | IP | Key Config |
|------|----|------------|
| pve | 192.168.1.10 | Primary node, GPU available (no passthrough) |
| pve2 | 192.168.1.11 | GPU passthrough (RTX 2080 Ti → Jellyfin VM), VFIO/IOMMU |

**Roles:** Networking, GPU passthrough, HA (qdevice on TrueNAS), hookscripts (GPU failover, VM monitor), iSCSI integration

### Monitoring (all hosts)

Every Linux VM gets:
- **node_exporter** — hardware/OS metrics (port 9100)
- **Vector** — log shipping to Loki via Vector Aggregator in K8s
- **Wazuh agent** — security monitoring
- **Unattended upgrades** — automated patching with per-host package blacklists

Proxmox hosts additionally get:
- **pve-exporter** — Proxmox API metrics (port 9221)
- **pve-quorum** — cluster quorum metrics via textfile collector

### Other VMs

| Host | IP | Purpose |
|------|----|---------|
| command-center1 | 192.168.1.88 | Ansible controller, kubectl, ArgoCD CLI, 1Password CLI |
| grafana | 192.168.1.121 | Grafana dashboards (port 3000) |
| npm | 192.168.1.150 | Nginx Proxy Manager (reverse proxy) |
| timescaledb | 192.168.1.122 | TimescaleDB (Docker, port 5432) |
| TrueNAS | 192.168.1.15 | NAS (NFS, iSCSI), corosync qdevice |

## Inventory

- **Dynamic:** `inventory.proxmox.yml`, auto-discovers VMs via Proxmox API (tag-based grouping)
- **Static:** `inventory.static.ini`, bare-metal/non-VM hosts (PVE nodes, TrueNAS)
- Proxmox API token sourced from `PROXMOX_TOKEN_SECRET` env var, decrypted from ansible-vault by `scripts/lib/proxmox-vault.sh` at wrapper start. See `docs/vault.md` for the rotation runbook and disaster-recovery bootstrap.

## Playbooks

```bash
ansible-playbook site.yml              # Run everything
ansible-playbook playbooks/jellyfin.yml         # Jellyfin VM only
ansible-playbook playbooks/monitoring.yml       # All monitoring agents
ansible-playbook playbooks/proxmox.yml          # PVE host config
ansible-playbook playbooks/proxmox-monitoring.yml  # PVE exporters + quorum
ansible-playbook playbooks/wazuh.yml            # Wazuh manager + agents
ansible-playbook playbooks/k8s_init.yml         # K8s cluster bootstrap
ansible-playbook playbooks/lb_setup.yml         # Load balancer pair
```

## Secrets

- **Ansible Vault** for encrypted variables (password from 1Password via `scripts/vault-pass.sh`). Includes the Proxmox API token (`vault_proxmox_api_token`).
- **1Password CLI cache** (`scripts/lib/op-secret-cache.sh`) for runtime-fetched secrets like Authentik, Grafana, Wazuh, Claude Bridge passwords. 12h TTL, kill-switched against rate-limit drains.
- **`scripts/lib/proxmox-vault.sh`** decrypts and exports `PROXMOX_TOKEN_SECRET` for dynamic inventory. Replaces the previous `op read` path that bypassed the env cache (issue #124).
- See `docs/vault.md` for variable inventory, rotation runbook, and disaster-recovery bootstrap.
- See `docs/op-call-inventory.md` for the per-call-site audit of every `op` invocation in the repo.

## Related Repos

| Repository | Purpose |
|------------|---------|
| [k8s-argocd](https://github.com/mithr4ndir/k8s-argocd) | Kubernetes manifests, ArgoCD GitOps |
| [terraform-quasarlab](https://github.com/mithr4ndir/terraform-quasarlab) | VM provisioning on Proxmox |
| [observability-quasarlab](https://github.com/mithr4ndir/observability-quasarlab) | Grafana dashboards and provisioning |
