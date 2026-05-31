# Jellyfin: move from GPU passthrough to CPU transcoding

- **Date:** 2026-05-31
- **Status:** Accepted, implemented
- **Affected:** `roles/jellyfin`, `roles/pve/hookscripts`,
  `terraform-quasarlab/proxmox/jellyfin`, Jellyfin Grafana dashboard

## Summary

Jellyfin (VM 115 on pve2) was transcoding via an RTX 2080 Ti passed through with
VFIO. We removed the GPU from the VM, switched Jellyfin to CPU (software)
transcoding on the Ryzen 9 5950X, and repurposed the 2080 Ti for a dedicated LLM
workload.

## The problem we never actually solved

The recurring incident was the RTX 2080 Ti **"falling off the bus"**:

```
NVRM: The NVIDIA GPU 0000:01:00.0 (PCI ID: 10de:1e07) installed in this
NVRM: system has fallen off the bus and is not responding to commands.
```

Once this happens the in-guest driver cannot talk to the card, `nvidia-smi`
returns exit 9, transcoding dies, and the only fix is a host-side PCI reset
(remove/rescan) plus a VM bounce.

This is **not** "Linux can't hold the GPU." The root cause is that a **consumer
GeForce card under VFIO passthrough is fragile**: its power-management and reset
behaviour across the VM boundary (D3cold / PCIe link state) is not validated for
passthrough the way datacenter cards are. The card is perfectly stable on bare
metal; it is the passthrough layer plus frequent VM-lifecycle churn (HA
start/stop, watchdog-driven module reloads, PCI resets) that wedges it.

We mitigated but never cured it:
- `pcie_aspm=off` was applied on pve2 (GRUB + live cmdline) on 2026-05-27.
- The card **still** fell off the bus four days later (2026-05-31).

So `pcie_aspm=off` reduces frequency but does not eliminate the failure. There is
no known config that makes consumer-GeForce VFIO passthrough reliable here.

## Why it kept paging us silently

Two recovery layers existed and **both had latent bugs** that made them
ineffective, which is why the only symptom we saw was a flapping
`ServiceInactive` alert rather than an actual recovery:

1. **Layer 1 — `gpu-watchdog.service` (VM-side).** The script ran with
   `set -euo pipefail`. Its GPU probe,
   `detected_gpu=$(timeout nvidia-smi ... | head -1 | tr -d '\r')`, returns
   exit 9 when the GPU is dead; under `pipefail` + `set -e` the failed
   assignment killed the script **before** it reached its own passive-mode /
   recovery logic. Result: an 8000+ restart crash-loop (exit 9) and a flapping
   alert, instead of recovery.

2. **Layer 2 — `gpu-vm-monitor.service` (pve2 host).** Its health check was
   `qm guest exec 115 -- nvidia-smi ... 2>&1` piped into `grep -qi "nvidia"`.
   A dead card prints *"NVIDIA-SMI has failed because it couldn't communicate
   with the NVIDIA driver"* — which contains "nvidia" — so the grep matched the
   card's own error message and scored a dead GPU as **healthy**. The monitor
   therefore never triggered the PCI reset it existed to perform.

The recovery system meant to make passthrough survivable had been quietly broken.

## Options considered

1. **Intel Arc + LXC bind-mount.** Most reliable transcode path, but neither
   host has an iGPU (pve = Xeon E5-2678 v3, pve2 = Ryzen 9 5950X non-G), so it
   required buying a discrete Arc card. Rejected: no purchase wanted.
2. **Keep passthrough, finish hardening** (fix the two bugs, add
   `disable_idle_d3`, etc.). Rejected: `pcie_aspm=off` already proved hardening
   only reduces frequency; the fragility is structural.
3. **CPU (software) transcoding. CHOSEN.** Removes the fragile layer entirely
   at zero cost and frees the 2080 Ti for a workload it is actually good at.

## Why CPU transcoding is sufficient here

Usage is direct-play heavy (LG TV direct-plays 4K over 5 GHz; desktop uses the
MPV-based Jellyfin Media Player). Transcoding is the exception. Measured on the
5950X with the stock `jellyfin-ffmpeg` (libx264, `veryfast`) against a real 4K
DV/HDR HEVC file:

| Transcode | Speed |
|-----------|-------|
| 4K HEVC -> 1080p H.264 (typical) | ~3.5x realtime |
| 4K HEVC -> 4K H.264 (worst case) | ~1.7x realtime |

Against a stated real load of 1-2 concurrent 4K streams (rare), this is
comfortable. The VM was bumped from 6 to 12 vCPUs (host has 16C/32T).

## What changed

- **VM 115:** removed `hostpci0-3` and the `gpu-failover` hookscript from the
  config; bumped cores 6 -> 12. No GPU is attached.
- **`roles/jellyfin`:** removed the NVIDIA driver install, GSP config,
  `ffmpeg-wrapper`, `gpu-watchdog`, and `gpu-metrics`; `encoding.xml` now uses
  `HardwareAccelerationType=none`; added teardown tasks that remove the obsolete
  artifacts from the host. Also dropped the long-completed Docker->native
  migration block.
- **`roles/pve/hookscripts`:** `pve_gpu_vms` emptied; the role now tears down
  `gpu-vm-monitor` and `gpu-failover` when no GPU VMs are configured.
- **terraform:** `cores` 6 -> 12 in `proxmox/jellyfin/locals.tf`.
- **observability:** removed the GPU rows (21 panels) from the Jellyfin Grafana
  dashboard. The generic `ServiceInactive` alert resolves on its own because the
  `gpu-watchdog` unit no longer exists.

## The 2080 Ti going forward

Repurposed for local LLM inference. To use it on the host (bare metal / LXC) the
vfio binding on pve2 must be reversed (remove `vfio-pci.ids` from the kernel
cmdline, unblacklist `nvidia`, install the driver), then expose `/dev/nvidia*`
into an LXC. A single long-lived driver binding is stable; it was the
passthrough + VM-lifecycle churn that caused the hangs, not the card.
