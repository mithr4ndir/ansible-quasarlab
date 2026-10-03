# roles/pbs

Installs and configures Proxmox Backup Server inside the `pbs` guests.

The VMs themselves (`pbs1` vmid 103 on pve, `pbs2` vmid 120 on pve2) are owned by
`terraform-quasarlab/proxmox/pbs`. This role never creates, resizes or moves
them. It configures what lives inside the guest.

## Why this exists

Backups used to live on `tank/backups`, which is to say on the pool they existed
to protect. That dataset was decommissioned in `terraform-quasarlab#26`, and
between that decommission and this role landing there are **no backups of any VM
in the lab**. Tracked as ansible-quasarlab#173 and #201.

## The one dangerous step

This role is the only thing in the repo that puts a filesystem on a raw disk.
Everything before the `mkfs` is a guard, and the guards encode a measured hazard
rather than general caution.

The datastore disk is `scsi1` on both guests, but the kernel enumerates the two
disks in opposite order. Verified 2026-10-03:

| guest | `scsi-0:0:0:0` | `scsi-0:0:0:1` |
|---|---|---|
| pbs1 | `sda`, root is `/dev/sda1` | `sdb`, datastore |
| pbs2 | `sdb`, root is `/dev/sdb1` | `sda`, datastore |

A role keyed on `/dev/sdb` formats pbs1's datastore and **pbs2's root disk**. So
the disk is resolved by its stable virtual SCSI LUN slot
(`/dev/disk/by-path/*-scsi-0:0:0:1`), and mounted by UUID, and four assertions
run before the `mkfs`:

1. exactly one disk occupies the LUN slot
2. it is not the device backing `/`
3. it measures the expected size within tolerance
4. it is blank, or already carries our own filesystem (the normal re-run case)

`tests/test_pbs_datastore_guards.py` asserts those guards run **before** the
`mkfs`, not merely that they exist. The tests were mutation-checked: six
deliberate breakages (hardcoding `/dev/sdb`, moving a guard after the `mkfs`,
dropping `--verify-new`, dropping `--keep-daily`, deleting the `mkfs`, moving the
mount check after datastore creation) are each caught by a named test.

Two of those tests were vacuous on the first pass, because `--verify-new` and the
LUN glob are both discussed in comments and a text search still matched after the
real argument was deleted. They now assert against parsed YAML. If you extend
this role, re-run the mutation check rather than trusting a green suite.

## Not vacuous by construction

The datastore is created with `--verify-new true`, so PBS re-reads every chunk
immediately after writing it, plus a scheduled `verify-job` for data already at
rest. This lab has shipped a vzdump job that went green while producing a
767-byte archive, because the disk carried `backup=0`. A green job is not
evidence; a verified read is. See `feedback_pve_disk_backup_flag_vacuous`.

`vm9000` (the ubuntu template) still carries `backup=0` and should stay excluded.

## Retention lives in prune.cfg, not datastore.cfg

The `--keep-*` and `--prune-schedule` arguments to `datastore create` are
accepted, then migrated by PBS into a separate prune job. So
`proxmox-backup-manager datastore show lab` lists only `gc-schedule` and
`verify-new`, and retention looks absent when it is not:

```
# proxmox-backup-manager prune-job list
default-lab-f7b1615e...  store lab  schedule hourly
  keep-hourly 24  keep-daily 7  keep-weekly 4  keep-monthly 3
```

Check `prune-job list` or `/etc/proxmox-backup/prune.cfg` before concluding that
retention was dropped.

## Schedules are systemd calendar events

Validated by a pre-flight `systemd-analyze calendar` task, because PBS reports a
bad value from inside `datastore create` as `unable to parse calendar event at
'daily' - Context("weekday")`, after the filesystem is already mounted.

`daily 03:30` is **not** valid: `hourly`, `daily` and `weekly` are complete
expressions and cannot take a time. Use a bare `03:30` for "every day at".

## Version pairing

PBS 3.x is the bookworm line and the correct pair for PVE 8.4. Both guests are
Debian 12. The bookworm `pbs-no-subscription` repo was confirmed live on
2026-10-03 offering `proxmox-backup-server 3.4.9-2`.

When the cluster moves to PVE 9 (Debian 13 trixie), PBS 4.x on trixie is the
matching release, and the keyring URL changes to
`proxmox-archive-keyring-trixie.gpg`. Revisit this together with the
ZFS-over-iSCSI storage plugin migration, which is itself a landmine: the held
`freenas-proxmox 2.4.0` fails the PVE 8 to 9 dist-upgrade via its dpkg trigger.
See `project_2026-09-21_thin_provisioning_and_pve9`.

## Collection dependency

Uses `community.general.filesystem` and `ansible.posix.mount`. Both are declared
by the open `#212` (fix/declare-used-collections), which also adds a test keeping
`requirements.yml` and the FQCNs in roles in step in both directions. This branch
deliberately does not touch `requirements.yml`, to avoid a pointless conflict with
that PR. If #212 has not landed when this merges, add `community.general` there.

## Still to do, deliberately not in this role

- **PVE-side registration.** A `pbs:` storage entry plus the backup job. Needs a
  PBS API token, which is a secret and belongs in the vault, so it is a separate
  change with its own review.
- **Replication.** `sync-job create` on pbs2 pulling from pbs1, so a full set
  exists on both physical hosts. This is the design in `terraform-quasarlab#26`.
- **Restore drill.** Until one VM has actually been restored from this datastore,
  the backup posture is unproven. The drill is the acceptance test for #173, not
  an optional extra.
