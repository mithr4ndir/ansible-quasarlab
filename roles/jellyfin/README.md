# Jellyfin Role

Deploys Jellyfin media server as a native systemd service with CPU (software)
transcoding and Prometheus monitoring.

## Architecture
Jellyfin runs natively on a Proxmox VM (not Docker, not K8s). Transcoding is
done in software (libx264/libx265) on the VM's CPU.

GPU transcoding was removed on 2026-05-31. The RTX 2080 Ti repeatedly "fell off
the bus" under VFIO passthrough (consumer GeForce + vfio power/reset
fragility), and `pcie_aspm=off` did not cure it. The card was repurposed for a
dedicated LLM workload. The Ryzen 9 5950X handles software transcoding
comfortably for this household's direct-play-heavy usage: a 4K HEVC to 1080p
H.264 transcode benchmarks at ~3.5x realtime, 4K to 4K at ~1.7x, against a
rare load of 1-2 concurrent 4K streams. See
`docs/decisions/2026-05-31-jellyfin-cpu-transcoding.md` for the full rationale.

## What This Role Does
1. **Jellyfin native install** — via official apt repository
2. **NFS media mount** — mounts the TrueNAS media share
3. **Encoding config** — software transcoding (`HardwareAccelerationType=none`)
4. **Monitoring** — Jellyfin API metrics via textfile collector
5. **GPU decommission** — removes the obsolete `gpu-watchdog`, `gpu-metrics`,
   `ffmpeg-wrapper`, and NVIDIA GSP config left over from the passthrough era

## Transcoding
- Software only: `HardwareAccelerationType` is `none`, `EnableHardwareEncoding`
  is false. Jellyfin invokes the stock `jellyfin-ffmpeg` directly (no wrapper).
- `EncodingThreadCount` is `-1` (ffmpeg auto-selects threads).
- Encourage direct play and cap remote client bitrate to avoid unnecessary 4K
  software transcodes.

## Metrics Exported
**Jellyfin** (60s interval): `jellyfin_active_streams`, `jellyfin_transcode_streams`,
`jellyfin_direct_play_streams`, `jellyfin_transcode_bitrate_bps`,
`jellyfin_library_movies_total`, `jellyfin_library_series_total`,
`jellyfin_library_episodes_total`

## Important Notes
- **Never copy `migrations.xml`** when upgrading — causes ObjectDisposedException on startup.
- **Never restart Jellyfin while a stream is active** — the SQLite library DB can corrupt mid-write.
- The host-side `pve/hookscripts` role no longer deploys the GPU failover
  hookscript or `gpu-vm-monitor`; both were decommissioned with the GPU.
