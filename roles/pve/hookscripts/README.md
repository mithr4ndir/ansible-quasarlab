# PVE Hookscripts Role

Deploys GPU management scripts to Proxmox VE hosts.

## Components

### GPU Failover Hookscript (`gpu-failover.sh`)
Automatically swaps PCI passthrough configuration when a GPU VM migrates between
Proxmox nodes. Each node has different PCI addresses for its GPU, so the VM config
must be updated before the VM starts on the new node.

**Key implementation detail**: Edits `/etc/pve/qemu-server/<vmid>.conf` directly
instead of using `qm set`, because `qm start` holds a file-level flock on the
config file. Calling `qm set` from a hookscript (which runs during `qm start`)
would deadlock. The `--skiplock` flag does NOT help — it only skips the Proxmox
config lock property, not the underlying flock.

### GPU VM Monitor (`gpu-vm-monitor.sh`)
Host-level health monitor that detects GPU failures inside VMs and performs
automatic PCI reset recovery. This is Layer 2 of a two-layer GPU recovery system.

**How it works**:
1. Runs every 30s via systemd timer on all PVE nodes
2. Checks GPU health inside VMs using `qm guest exec` (QEMU guest agent)
3. After 3 consecutive failures: stops VM → resets PCI devices → starts VM
4. PCI device reset (`echo 1 > /sys/bus/pci/devices/<addr>/reset`) is the only
   way to recover a hung GPU under PCI passthrough

**Layer 1** (VM-side) is the `gpu-watchdog.service` inside the Jellyfin VM, which
attempts soft recovery (nvidia module reload) before the host monitor intervenes.

## Metrics (Prometheus via node_exporter textfile collector)
- `gpu_vm_monitor_healthy{vmid}` — 1 if GPU responding, 0 if failed
- `gpu_vm_monitor_consecutive_failures{vmid}` — current failure streak (resets at 3)
- `gpu_vm_monitor_pci_resets_total` — cumulative PCI reset recoveries

## Configuration
GPU VMs and their PCI addresses are defined in `defaults/main.yml` (`pve_gpu_vms`).

## Known Issues
- `nvidia-smi` hangs indefinitely when the GPU crashes — all calls must use `timeout`
- HDR10 tone mapping in Jellyfin (issue #8400) is a known trigger for GPU crashes
- GSP firmware must be disabled for PCI passthrough (`NVreg_EnableGpuFirmware=0`)
- VM reboot alone does NOT fix a crashed GPU — PCI reset on the host is required
