# truenas

Appliance-specific configuration for the TrueNAS SCALE host. Runs from
`playbooks/monitoring.yml`, guarded by `when: "'nas' in group_names"`.

## Why this role exists separately

TrueNAS SCALE is not a general Linux VM. `/` is read-only ZFS, `/usr` is not
writable, and Vector cannot be installed, which is why `monitoring/vector` and
`unattended_upgrades` are both explicitly skipped for the `nas` group. This
role covers what that host still needs, using only the mechanisms the
appliance leaves open.

## What it does

| Area | Mechanism | Durability |
|---|---|---|
| Pool, per-disk ZFS + SMART metrics | script + systemd timer writing into node_exporter's textfile dir | boot environment only, see below |
| SMART enable | `smartctl -s on` per disk, checked first | drive firmware, persists |
| Remote syslog | `midclt call system.advanced.update` | TrueNAS config DB, survives upgrades |

### Metrics

`node_exporter` exports ZFS ARC internals and per-dataset I/O counters, but no
pool capacity, no pool health and no SMART. `zfs-smart-textfile.sh` fills that
in by writing `zfs_pool_*` and `smart_*` series into the directory
`node_exporter` was already started with, so no second exporter is needed. The
file is written atomically because `node_exporter` reads the directory on every
scrape and a partial file surfaces as a parse error.

Consumed by the `NAS: TrueNAS storage and drive health` Grafana dashboard in
k8s-argocd.

#### Per-disk ZFS state and bay labels

`zfs_pool_health` is not enough on its own. On 2026-10-08 a `tank` mirror member
was FAULTED for half an hour while `zpool list` kept reporting the pool ONLINE,
so the pool-level alert never fired. The script therefore also parses
`zpool status -j -p` and writes, per leaf vdev:

| Series | Meaning |
|---|---|
| `zfs_vdev_state` | 0=ONLINE 1=DEGRADED 2=FAULTED 3=OFFLINE 4=UNAVAIL 5=REMOVED 6=UNKNOWN (spare AVAIL/INUSE count as 0) |
| `zfs_vdev_read_errors`, `_write_errors`, `_checksum_errors` | ZFS error counters, reset by `zpool clear` |
| `zfs_vdev_scrape_ok` | 0 when `zpool status -j` could not be read, so silence is not mistaken for health |

Every `smart_*` and `zfs_vdev_*` series carries `bay` and `serial`, because the
`sdX` letter changes between boots and an alert that only says `sdd` does not
tell anyone which tray to pull.

- `bay` is N for kernel port `ataN`, which on the DXP8800 Plus is the Nth tray
  from the left (verified by LED and a live reseat, see
  quasarlab-disaster-recovery `architecture/storage.md`). NVMe devices are `m2`.
- `serial` comes from the VPD page the kernel cached when the disk attached
  (`/sys/class/block/<dev>/device/vpd_pg80`, NVMe: `device/serial`). Reading it
  sends no I/O to the drive, so it still works when the drive has stopped
  answering, which is when `smartctl` cannot report a serial.
- When a disk detaches, its by-partuuid link and sysfs node disappear. The last
  known bay and serial per vdev guid are kept in
  `<textfile dir>/.zfs_vdev_labels` (not `*.prom`, so `node_exporter` ignores
  it) and used as a fallback.

### SMART

Five of the eight `tank` drives, every Inland, had SMART **disabled at the
controller**. `smartctl` returned no attributes, so `smartd` had nothing to act
on and the TrueNAS alert page reported no alerts. That was true and useless at
the same time, for 5 of 8 drives in the pool holding every VM disk.

The role checks each disk and only enables where it is off, so a converged run
reports no change.

### Syslog

There is no writable `/etc` for a syslog drop-in, but `system.advanced` keeps
the remote target in TrueNAS's own config database, which survives both reboots
and version upgrades.

Level is `F_NOTICE` on purpose. At `F_INFO` the zettarepl snapshot-retention
chatter alone produced roughly 3,700 lines per minute, two orders of magnitude
more than any other host in the lab. `F_NOTICE` keeps kernel, ZFS and
middleware errors and drops that. This was checked by emitting one line at each
severity and querying Loki for each: `info` filtered, `notice`, `warning`,
`err` and `crit` all delivered.

## Known limitation

The script and the systemd units live under `/var/lib/node_exporter` and
`/etc/systemd/system`, both on `boot-pool/ROOT/<version>`, a **version-specific
boot environment**. A TrueNAS version upgrade creates a new BE and they will not
carry over. `node_exporter` itself already has exactly this exposure, which is
why this role sits alongside it: one `monitoring.yml` run after an upgrade
restores both. The syslog setting is in the config DB and is not affected.
