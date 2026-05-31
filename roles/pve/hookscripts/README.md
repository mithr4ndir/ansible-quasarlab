# PVE Hookscripts Role

Deploys GPU passthrough management scripts to Proxmox VE hosts, **only when GPU
passthrough VMs are configured** (`pve_gpu_vms` is non-empty). When it is empty,
the role tears the machinery down instead.

## Status (2026-05-31)

`pve_gpu_vms` is currently **empty**. Jellyfin (VM 115) was the only GPU
passthrough VM; it was moved to CPU transcoding and the RTX 2080 Ti was
repurposed for an LLM workload after recurring "fell off the bus" hangs under
VFIO passthrough. With no GPU VMs, the role stops and removes the
`gpu-vm-monitor` timer/service and the `gpu-failover` snippet. Re-add an entry
to `pve_gpu_vms` to bring passthrough failover + recovery back.

Note: the two recovery layers had latent bugs that made them ineffective (the
VM-side `gpu-watchdog` crash-looped under `set -e` instead of recovering; this
host monitor's health check `grep`-matched `nvidia-smi`'s own error string and
scored a dead GPU as healthy). If passthrough is ever reinstated, fix those
before relying on them. See
`docs/decisions/2026-05-31-jellyfin-cpu-transcoding.md`.

## Components (deployed only when `pve_gpu_vms` is non-empty)

### GPU Failover Hookscript (`gpu-failover.sh`)
Swaps PCI passthrough configuration when a GPU VM migrates between Proxmox
nodes. Each node has different PCI addresses for its GPU, so the VM config must
be updated before the VM starts on the new node.

**Key implementation detail**: edits `/etc/pve/qemu-server/<vmid>.conf` directly
instead of using `qm set`, because `qm start` holds a file-level flock on the
config file. Calling `qm set` from a hookscript (which runs during `qm start`)
would deadlock. The `--skiplock` flag does NOT help — it only skips the Proxmox
config lock property, not the underlying flock.

### GPU VM Monitor (`gpu-vm-monitor.sh`)
Host-level health monitor that detects GPU failures inside VMs and performs
automatic PCI reset recovery (Layer 2 of the old two-layer recovery system).

**How it worked**:
1. Ran every 30s via systemd timer
2. Checked GPU health inside VMs using `qm guest exec` (QEMU guest agent)
3. After 3 consecutive failures: stop VM → reset PCI devices → start VM

## Metrics (only while a GPU VM is configured)
- `gpu_vm_monitor_healthy{vmid}` — 1 if GPU responding, 0 if failed
- `gpu_vm_monitor_consecutive_failures{vmid}` — current failure streak (resets at 3)
- `gpu_vm_monitor_pci_resets_total` — cumulative PCI reset recoveries

## Configuration
GPU VMs and their PCI addresses are defined in `defaults/main.yml`
(`pve_gpu_vms`). Empty means "no passthrough VMs; tear the machinery down".
