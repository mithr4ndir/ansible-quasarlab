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

## Two things that only a real backup reveals

**The datastore root must belong to `backup`.** `proxmox-backup-proxy` runs as
the `backup` user and creates `vm/<vmid>/` at the top level of the datastore.
PBS creates `.chunks` with the right ownership itself, because the manager CLI
runs as root, so a root-owned datastore root looks fine until the first backup
fails with:

```
backup connect failed: command error: Permission denied (os error 13)
```

which names neither the path nor the user. The chown must run *after* the mount,
since a mount point's ownership is masked by the filesystem mounted over it.

**A token's privileges are the intersection of its own ACL and its user's.**
Granting roles only to `pve@pbs!pve` and not to `pve@pbs` resolves to an empty
permission set. The symptom is confusing: the token authenticates with HTTP 200
and the datastore API returns `{"data":[]}` rather than a 403, so `pvesm add
pbs` reports

```
create storage failed: pbs-lab: Cannot find datastore 'lab', check permissions
and existence!
```

as though the datastore were missing. `proxmox-backup-manager user permissions
'pve@pbs!pve' --path /datastore/lab` showing nothing is the tell. Both identities
need the roles, and `DatastoreAudit` is needed on top of `DatastoreBackup`
because `pvesm add` validates by enumerating the datastore.

The role asserts the resolved privileges rather than trusting that `acl update`
returned 0, because it returns 0 either way.

## Retention has exactly one owner

PBS owns it, via its own hourly prune job. The PVE backup job deliberately sets
no `prune-backups`, so there is one policy rather than two pruning the same
datastore, and the PVE token does not need `Datastore.Prune`.

## Restore drill, 2026-10-06

nginx2 (vm113) backed up in 1m16s, 1.05 GiB stored, 89% of the disk detected as
zero and skipped. Snapshot verified on write (`verification: state ok`).
Restored to a scratch VMID on local `SSD1` in 6.7s, booted with its NIC
link-down to avoid colliding with the live host's IP, and compared:

| marker | live | restored |
|---|---|---|
| hostname | nginx2 | nginx2 |
| kernel | 6.8.0-146-generic | 6.8.0-146-generic |
| root fs | 51G / 3.1G / 6% | 51G / 3.1G / 6% |
| packages | 687 | 687 |
| `/etc` manifest md5 | 5dc3ba9f8c06bf03 | 5dc3ba9f8c06bf03 |

Scratch VM destroyed afterwards, no stray volumes.

Incidentally: nginx2 runs no nginx. It is the kube-apiserver LB, listening on
6443. The name is historical, and the restore did not lose anything.

## Replication: pbs2 pulls from pbs1

Until this existed every restore point in the lab sat on pbs1. The hourly
vzdump job excludes `102,103,120,9000,9001,9002`, so the two backup servers
back up neither themselves nor each other, and losing one VM lost every
backup. pbs1 (vmid 103) runs on `pve` and pbs2 (vmid 120) on `pve2`, so one
Proxmox host failure cannot take both.

The direction is a **pull**, and that is the security argument for the whole
design. A push would require a credential on pbs1 that can WRITE to pbs2, so
whatever compromises or corrupts the primary reaches the copy. With a pull,
pbs1 holds no credential for pbs2 at all, and the one credential that does
exist (`sync@pbs!pbs2`, held by the replica) is `DatastoreReader`: it cannot
write to, prune, or delete anything on the primary.

### remove-vanished is false, on purpose

A sync with `remove-vanished` true is a mirror, not a backup. Anything deleted
on pbs1 is deleted here on the next run, so a wiped, ransomed or mis-pruned
primary replicates faithfully into the only remaining copy and the lab is back
to one set of backups in two places. False means the replica only ever gains
snapshots. Its own prune job, created by this role for its own datastore with
the same `keep-*` policy, is what bounds growth, which also means retention
here does not depend on pbs1's retention being right.

`roles/pbs/tests/test_pbs_replication.py` fails if this flips, on both the
create and the update path.

### Bootstrap, which is not fully idempotent

`proxmox-backup-manager user generate-token` reveals the secret exactly once,
so it cannot be a task, the same reason `pve@pbs!pve` is an operator step. The
role creates the user, grants the ACLs to both the user and the token, and
then asserts the END STATE, because `user permissions` on a token that does
not exist returns rc 0 and an empty list. The sequence is:

```
# 1. pbs1: create the reader user and its ACLs. This FAILS at the final
#    assertion, by design, because the token does not exist yet.
ansible-playbook playbooks/pbs.yml --limit pbs1

# 2. pbs1: mint the token. Record the secret in 1Password as
#    "PBS sync@pbs!pbs2 token" (Infrastructure vault). It is shown once.
proxmox-backup-manager user generate-token sync@pbs pbs2

# 3. pbs1: re-run. The assertion now passes.
ansible-playbook playbooks/pbs.yml --limit pbs1

# 4. pbs2: configure the remote and the sync job.
export PBS_SYNC_TOKEN_SECRET='...'   # from 1Password, never committed
ansible-playbook playbooks/pbs.yml --limit pbs2
```

The task that consumes the secret is `no_log: true`, because it passes it as an
argv element and the run output goes to the ara database and
`/var/log/ansible-quasarlab`. A test asserts that too.

### What the role will not reconcile

- **The remote's password.** PBS never returns it, so there is nothing to
  compare and a blind update would rewrite it every run. Rotating the token is
  a manual `proxmox-backup-manager remote update pbs1 --password <secret>`.
- **A sync job's remote or datastore.** PBS cannot repoint a job in place, so a
  mismatch fails loudly instead of being silently tolerated. Remove the job and
  let the role recreate it.

### The fingerprint is pinned

`pbs_sync_remotes[].fingerprint` is pbs1's TLS certificate, from
`proxmox-backup-manager cert info`. Pinning it is what stops the pull being
answered by something else at that address, which is not hypothetical in a lab
where sixty QDevice reboots once moved seven VMs onto each other's leases.
Regenerating pbs1's certificate breaks replication until this value is
updated. That is the intended behaviour, not a bug.

### Failover is manual

pbs2 sets `pbs_configure_pve_client: false`, so it has no `pve@pbs!pve`
identity. Promoting it means generating a token there (the secret cannot be
chosen, so it will differ from pbs1's) and repointing the PVE storage. The
replica was deliberately not given a standing write credential just to make
failover one step shorter.

## Still to do, deliberately not in this role

- **PVE-side registration.** A `pbs:` storage entry plus the backup job. Needs a
  PBS API token, which is a secret and belongs in the vault, so it is a separate
  change with its own review.
- **Restore drill.** Until one VM has actually been restored from this datastore,
  the backup posture is unproven. The drill is the acceptance test for #173, not
  an optional extra.
