# Jellyfin Role

Deploys Jellyfin media server as a native systemd service with NVIDIA GPU
hardware transcoding, GPU crash recovery, and Prometheus monitoring.

## Architecture
Jellyfin runs natively on a Proxmox VM (not Docker, not K8s) with PCI
passthrough for the GPU. This eliminates the NVIDIA container runtime
failure mode where Docker hangs indefinitely when the GPU crashes.

## What This Role Does
1. **NVIDIA driver** — installs driver and disables GSP firmware (required for PCI passthrough)
2. **Docker cleanup** — stops and removes any existing Docker-based Jellyfin
3. **Jellyfin native install** — via official apt repository
4. **Data migration** — one-time migration from Docker layout to native paths (library.db path rewrite)
5. **NFS media mount** — mounts TrueNAS media share
6. **Encoding config** — NVENC hardware transcoding with tone mapping disabled
7. **GPU watchdog** — Layer 1 recovery: nvidia module reload on GPU failure
8. **Monitoring** — GPU metrics + Jellyfin API metrics via textfile collector

## GPU Watchdog (Layer 1 Recovery)
The `gpu-watchdog.service` runs inside the VM and:
- Checks `nvidia-smi` every 30s (with 10s timeout — nvidia-smi hangs on GPU crash)
- On failure: stops Jellyfin, kills ffmpeg, unloads/reloads nvidia modules
- After 2 failed soft recovery attempts, waits for host-side PCI reset (Layer 2)
- Layer 2 is the `gpu-vm-monitor` on the Proxmox host (see `pve/hookscripts` role)

## Metrics Exported
**GPU** (30s interval): `nvidia_gpu_utilization_percent`, `nvidia_gpu_encoder_utilization_percent`,
`nvidia_gpu_decoder_utilization_percent`, `nvidia_gpu_memory_used_mib`,
`nvidia_gpu_temperature_celsius`, `nvidia_gpu_fan_speed_percent`,
`nvidia_gpu_power_draw_watts`

**Jellyfin** (60s interval): `jellyfin_active_streams`, `jellyfin_transcode_streams`,
`jellyfin_direct_play_streams`, `jellyfin_transcode_bitrate_bps`,
`jellyfin_library_movies_total`, `jellyfin_library_series_total`,
`jellyfin_library_episodes_total`

**Watchdog**: `nvidia_gpu_failures_total`, `nvidia_gpu_soft_recoveries_total`,
`nvidia_gpu_hard_recoveries_total`

## Important Notes
- **Tone mapping is disabled** — Jellyfin issue #8400 causes GPU crashes under PCI passthrough
- **Never copy `migrations.xml`** when upgrading — causes ObjectDisposedException on startup
- **Data migration uses `cp -a`** (not `cp -rn`) — apt postinst creates empty files that block no-clobber copy
