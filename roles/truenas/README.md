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
| Bay LEDs | `bay-leds.service` driving the pinned `ugreen_leds_cli` over I2C | boot environment only |
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

### Bay LEDs

`bay-leds.sh` drives each front-panel bay LED (bay N = kernel `ataN` = Nth tray
from the left):

| LED | Meaning |
|---|---|
| dim white | disk present, idle |
| white blink | disk reading or writing (held 1s after the last I/O, so ZFS flush bursts do not flap) |
| amber blink | ZFS counted read/write/checksum errors on it (until `zpool clear`), or SMART cannot be read |
| red | its vdev is not ONLINE, including a disk that has dropped off the bus: the tray to pull |
| off | empty bay |
| purple slow pulse | the service is stopped, or the exporter's data is over 5 minutes old: do not trust the panel |

Fault state is read from `zfs_smart.prom`, so red appears within about a minute
and the service never runs `zpool` or touches a drive. Activity comes from
`/sys/class/block/<dev>/stat`. An LED is written only when its state changes,
because the controller shares the I2C bus with board sensors. The built-in
sweep animation does not run while this service owns the LEDs.

To light a tray by hand during a drive swap, pause the service first:

```
sudo touch /run/bay-leds.hold                      # service stops writing
sudo /var/lib/node_exporter/ugreen_leds_cli disk3 -color 0 0 255 -blink 400 400
sudo rm /run/bay-leds.hold                         # service repaints every bay
```

It writes `bay_leds_last_loop_timestamp_seconds` to the textfile directory so
a dead service alerts in Alertmanager rather than freezing the panel unnoticed.

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

## The UGREEN IT8613 watchdog

The DXP8800 Plus arms a 180s hardware watchdog that its stock UGOS services and
TrueNAS does not, so the board hard-resets roughly every three minutes. That is
the 15 hour full-lab outage of 2026-09-20: the NAS rebooted about sixty times,
and the QDevice VM took a fresh random MAC each boot, which exhausted DHCP
leases and silently moved seven VMs off their addresses, including both k8s
control-plane nodes and the reverse proxy. Nothing alerted.

It is not the Intel TCO watchdog. Unloading `iTCO_wdt` still reset, and holding
`/dev/watchdog` open and feeding it every 5s still reset at ~161s. Time to
death was identical idle, with 12 CPUs pegged, and with both 10G NICs unbound,
which rules out power, thermal, VRM, RAM and the NICs together and rules in a
firmware timer. Super I/O LDN 0x07 register `0x73` read `0xb4`, which is 180,
exactly the observed cadence. `it87_wdt` sees the chip (`Chip 8613 Revision
000c`) but will not bind: IT8613 support landed in kernel 6.7 and TrueNAS runs
6.6.44.

### Two persistence layers, neither redundant

Super I/O registers are volatile and BIOS re-arms 180 on every boot, so the
disable must run every boot.

| layer | why it alone is not enough |
|---|---|
| `ugreen-wdt-disable.service` | runs early enough to beat the ~161s deadline, but lives in `/etc`, which a TrueNAS version upgrade wipes |
| TrueNAS POSTINIT entry | lives in the config database and survives upgrades, but is not what makes the early deadline |

The role installs boot-time mechanisms rather than performing the register
write itself. If Ansible never runs again the NAS still has to disarm its own
watchdog on boot.

### initshutdownscript.create is not idempotent

It appends without checking, so an unguarded task adds one POSTINIT entry per
run and the hourly timer would accumulate them forever, each executing at boot.
The role queries first and creates only when absent, asserts loudly if
duplicates already exist, and re-enables an entry that was switched off (which
is the state that is otherwise invisible: present in the query, so no duplicate
is created, and inert at boot).

### What is asserted

The exit status of the service, not whether it is `active`. The script refuses
to write unless the chip identifies as `0x8613`, and exits non-zero if register
`0x73` does not read back `0`, so the exit status is the only thing that says
the timer is actually disarmed. A unit that ran and cleared nothing is `active`
too.

`roles/truenas/tests/test_ugreen_watchdog.py` executes the real script against
a stub Super I/O rather than adding a test hook to it, because it is the most
dangerous file here to get wrong. It covers the unlock sequence, the logical
device select, the chip guard refusing an IT8728, and the read-back failing
when a register will not clear.

The proper fix remains disabling Watchdog Timer in BIOS, which needs physical
access.
