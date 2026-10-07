"""Regression tests for the destructive step in roles/pbs.

Run from the repo root:
    python3 -m pytest tests/test_pbs_datastore_guards.py -rs
    python3 -m unittest discover tests

roles/pbs puts a filesystem on a raw disk, which is the only genuinely
destructive action in this repo. The hazard is specific and measured, not
hypothetical: the datastore disk is scsi1 on both PBS guests, but the kernel
enumerates them in opposite order (verified 2026-10-03):

    pbs1  scsi-0:0:0:0 -> sda (root=/dev/sda1)   scsi-0:0:0:1 -> sdb
    pbs2  scsi-0:0:0:0 -> sdb (root=/dev/sdb1)   scsi-0:0:0:1 -> sda

So a role keyed on /dev/sdb formats pbs1's datastore and pbs2's ROOT disk.

These tests assert the guards exist AND that they run before the mkfs. A guard
that runs after the filesystem is created is decoration, and an ordering bug
would otherwise pass every "is the assertion present" style check. That failure
mode has bitten this repo before, where a test asserting template source passed
against the live bug.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
TASKS = REPO / "roles" / "pbs" / "tasks" / "main.yml"
DEFAULTS = REPO / "roles" / "pbs" / "defaults" / "main.yml"

# A bare kernel device name used as a value, e.g. "/dev/sda" or "/dev/sdb1".
# The role must address disks by /dev/disk/by-path or by UUID instead.
BARE_SD_DEVICE = re.compile(r"/dev/sd[a-z]")


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _index_of(tasks: list[dict], predicate) -> int:
    for i, task in enumerate(tasks):
        if predicate(task):
            return i
    return -1


def _is_mkfs(task: dict) -> bool:
    return "community.general.filesystem" in task


def _assert_tasks(tasks: list[dict]) -> list[tuple[int, dict]]:
    return [
        (i, t) for i, t in enumerate(tasks) if "ansible.builtin.assert" in t
    ]


class PbsDatastoreGuards(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(TASKS.is_file(), f"missing {TASKS}")
        self.tasks = _tasks()
        self.mkfs_index = _index_of(self.tasks, _is_mkfs)

    def test_role_creates_a_filesystem_at_all(self) -> None:
        """Sanity: if the mkfs disappears, every ordering test below is vacuous."""
        self.assertNotEqual(
            self.mkfs_index, -1,
            "no community.general.filesystem task found; the ordering "
            "assertions in this file would silently pass against anything",
        )

    def test_no_bare_kernel_device_names_anywhere(self) -> None:
        """The whole point: /dev/sdX is not stable across pbs1 and pbs2."""
        for path in (TASKS, DEFAULTS):
            text = path.read_text()
            # Strip comment lines; the hazard is documented there on purpose.
            code = "\n".join(
                line for line in text.splitlines()
                if not line.lstrip().startswith("#")
            )
            found = BARE_SD_DEVICE.findall(code)
            self.assertEqual(
                found, [],
                f"{path.name} references bare kernel device(s) {found}. "
                "Use /dev/disk/by-path/*-scsi-0:0:0:1 or a UUID: on pbs2 "
                "/dev/sda is the ROOT disk.",
            )

    def _datastore_create_argv(self) -> list[str]:
        """The argv of the `datastore create` task, as a list of strings.

        Parsed from YAML rather than grepped out of the file: the LUN glob and
        the --verify-new flag are both discussed in comments, so a text search
        passes happily after the real argument has been deleted. A mutation run
        caught exactly that, so these assertions go through the parser.
        """
        for task in self.tasks:
            cmd = task.get("ansible.builtin.command")
            if not isinstance(cmd, dict):
                continue
            argv = [str(a) for a in cmd.get("argv", [])]
            if "datastore" in argv and "create" in argv:
                return argv
        self.fail("no `datastore create` task with an argv list found")

    def test_disk_is_resolved_by_stable_lun_slot(self) -> None:
        find = next(
            (t["ansible.builtin.find"] for t in self.tasks
             if "ansible.builtin.find" in t),
            None,
        )
        self.assertIsNotNone(find, "no find task resolving the datastore disk")
        self.assertEqual(
            find.get("patterns"), "*-scsi-0:0:0:1",
            "the find task must match the scsi1 LUN slot; comments mentioning "
            "the slot do not resolve a device",
        )
        self.assertEqual(
            find.get("paths"), "/dev/disk/by-path",
            "must search /dev/disk/by-path, the only stable naming here",
        )

    def test_mkfs_does_not_force_or_resize(self) -> None:
        mkfs = self.tasks[self.mkfs_index]["community.general.filesystem"]
        self.assertNotEqual(
            mkfs.get("force"), True,
            "force=true would overwrite an existing filesystem, defeating the "
            "blank-or-ours guard",
        )
        self.assertNotEqual(
            mkfs.get("resizefs"), True,
            "resizefs=true silently grows the datastore on any disk resize",
        )

    def test_lun_link_is_resolved_via_lnk_source_not_path(self) -> None:
        """Regression: follow:true made stat report the path it was given.

        The first run of this role failed with
            lsblk: /dev/pci-0000:01:02.0-scsi-0:0:0:1: not a block device
        because ansible.builtin.stat with follow:true reports `stat.path` as the
        input path, not the resolved target. The canonical device is in
        `stat.lnk_source`, which is only populated when the link is NOT followed.
        """
        stat_tasks = [
            t for t in self.tasks
            if "ansible.builtin.stat" in t
            and "pbs_lun_links" in str(t["ansible.builtin.stat"].get("path", ""))
        ]
        self.assertEqual(
            len(stat_tasks), 1,
            "expected exactly one stat task resolving the LUN link",
        )
        stat_args = stat_tasks[0]["ansible.builtin.stat"]
        self.assertFalse(
            stat_args.get("follow", False),
            "stat must NOT follow the link: with follow:true, stat.path is the "
            "input path and lnk_source is absent, which yielded a non-block "
            "device at runtime",
        )

        device_fact = next(
            (t["ansible.builtin.set_fact"] for t in self.tasks
             if "ansible.builtin.set_fact" in t
             and "pbs_datastore_device" in t["ansible.builtin.set_fact"]),
            None,
        )
        self.assertIsNotNone(device_fact, "pbs_datastore_device is never set")
        expr = str(device_fact["pbs_datastore_device"])
        self.assertIn(
            "lnk_source", expr,
            f"pbs_datastore_device is built from {expr!r}; it must come from "
            "stat.lnk_source, the resolved target",
        )
        self.assertNotIn(
            "stat.path", expr,
            "stat.path is the input path, not the resolved device",
        )

    def test_block_device_guard_runs_before_the_root_disk_guard(self) -> None:
        """A malformed path makes the root-disk guard pass for the wrong reason.

        '/dev/sda1'.startswith('/dev/pci-0000:...') is false, so "not the root
        disk" passes on nonsense. The block-device check has to come first or
        the root-disk guard proves nothing.
        """
        def guard_index(needle: str) -> int:
            for i, t in enumerate(self.tasks):
                if "ansible.builtin.assert" not in t:
                    continue
                if needle in yaml.safe_dump(t["ansible.builtin.assert"]):
                    return i
            return -1

        blockdev = guard_index("isblk")
        root = guard_index("pbs_root_source.stdout")
        self.assertNotEqual(
            blockdev, -1,
            "no assertion that the resolved path is actually a block device",
        )
        self.assertNotEqual(root, -1, "no root-disk guard")
        self.assertLess(
            blockdev, root,
            f"block-device check at task {blockdev} must precede the root-disk "
            f"guard at task {root}; otherwise an unresolved path satisfies the "
            "root-disk guard trivially",
        )

    def test_four_guards_all_run_before_the_mkfs(self) -> None:
        """Each guard must precede the destructive step, not merely exist.

        Keyed on a distinctive substring of each assert's `that`/`fail_msg` so
        that reordering the role breaks the test.
        """
        required = {
            "single LUN occupant": "length == 1",
            "not the root disk": "pbs_root_source.stdout",
            "expected size": "pbs_datastore_expected_gb",
            "blank or already ours": "pbs_existing_fs.stdout",
        }
        asserts = _assert_tasks(self.tasks)
        self.assertTrue(asserts, "role contains no assert tasks at all")

        for label, needle in required.items():
            positions = [
                i for i, t in asserts
                if needle in yaml.safe_dump(t["ansible.builtin.assert"])
            ]
            with self.subTest(guard=label):
                self.assertTrue(
                    positions,
                    f"no assert guarding '{label}' (looked for {needle!r})",
                )
                self.assertLess(
                    min(positions), self.mkfs_index,
                    f"guard '{label}' runs at task {min(positions)} but the "
                    f"filesystem is created at task {self.mkfs_index}; a guard "
                    "after the mkfs protects nothing",
                )

    def test_mount_is_verified_before_datastore_creation(self) -> None:
        """A datastore over an unmounted dir fills the 32G root disk instead.

        Targets the findmnt task whose failed_when compares against the
        resolved datastore device. Matching merely on "findmnt" picks up the
        earlier root-filesystem lookup instead, which sits before the create
        no matter where the real check goes: a mutation run proved that version
        of this test could not fail.
        """
        def is_datastore_mount_check(task: dict) -> bool:
            cmd = str(task.get("ansible.builtin.command", ""))
            if "findmnt" not in cmd or "pbs_datastore_mount" not in cmd:
                return False
            return "pbs_datastore_device" in str(task.get("failed_when", ""))

        mount_check = _index_of(self.tasks, is_datastore_mount_check)
        create = _index_of(
            self.tasks,
            lambda t: isinstance(t.get("ansible.builtin.command"), dict)
            and "create" in [str(a) for a in
                             t["ansible.builtin.command"].get("argv", [])]
            and "datastore" in [str(a) for a in
                                t["ansible.builtin.command"].get("argv", [])],
        )
        self.assertNotEqual(
            mount_check, -1,
            "no findmnt check asserting the datastore mount resolves to the "
            "resolved device",
        )
        self.assertNotEqual(create, -1, "no datastore create task")
        self.assertLess(
            mount_check, create,
            f"the datastore mount is verified at task {mount_check} but PBS is "
            f"pointed at the path at task {create}; creating a datastore over "
            "an unmounted directory silently fills the 32G root disk",
        )

    def test_new_backups_are_verified_on_write(self) -> None:
        """--verify-new is what separates 'job went green' from 'data readable'."""
        argv = self._datastore_create_argv()
        self.assertIn(
            "--verify-new", argv,
            "datastore must be created with --verify-new so PBS re-reads each "
            "chunk after writing; this lab has shipped a green vzdump job that "
            "produced a 767-byte archive",
        )
        self.assertEqual(
            argv[argv.index("--verify-new") + 1], "true",
            "--verify-new must be enabled, not merely present",
        )

    def test_retention_is_actually_passed_to_the_datastore(self) -> None:
        """Retention on the datastore is the floor if a PVE job is misconfigured."""
        argv = self._datastore_create_argv()
        for flag in ("--keep-hourly", "--keep-daily", "--keep-weekly",
                     "--keep-monthly", "--gc-schedule", "--prune-schedule"):
            with self.subTest(flag=flag):
                self.assertIn(flag, argv, f"{flag} missing from datastore create")


if __name__ == "__main__":
    unittest.main()


class PbsScheduleDefaults(unittest.TestCase):
    """The default schedules must be valid systemd calendar events.

    `daily 03:30` looked reasonable and is rejected: the shorthands are complete
    expressions and cannot take a time. PBS only surfaces that from inside
    `datastore create`, after the filesystem is mounted, so it is worth catching
    here.
    """

    def setUp(self) -> None:
        self.defaults = yaml.safe_load(DEFAULTS.read_text())

    def test_schedules_parse(self) -> None:
        import shutil
        import subprocess

        analyzer = shutil.which("systemd-analyze")
        if analyzer is None:
            self.skipTest("systemd-analyze not available to validate calendars")

        for key in ("pbs_gc_schedule", "pbs_prune_schedule",
                    "pbs_verify_schedule"):
            value = self.defaults[key]
            with self.subTest(schedule=key, value=value):
                result = subprocess.run(
                    [analyzer, "calendar", str(value)],
                    capture_output=True, text=True,
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"{key}={value!r} is not a valid systemd calendar event: "
                    f"{result.stderr.strip()}",
                )


class PbsDatastoreOwnership(unittest.TestCase):
    """proxmox-backup-proxy runs as `backup` and writes vm/<vmid>/ at the root.

    A root-owned datastore root fails every backup with
        backup connect failed: command error: Permission denied (os error 13)
    which names neither the path nor the user. PBS fixes .chunks itself because
    the manager CLI runs as root, which masks the problem until a real backup
    is attempted.
    """

    def setUp(self) -> None:
        self.tasks = _tasks()

    def _index(self, predicate) -> int:
        return _index_of(self.tasks, predicate)

    def test_datastore_root_is_given_to_the_backup_user(self) -> None:
        owner_task = next(
            (t for t in self.tasks
             if "ansible.builtin.file" in t
             and t["ansible.builtin.file"].get("path") == "{{ pbs_datastore_mount }}"
             and t["ansible.builtin.file"].get("owner") == "backup"),
            None,
        )
        self.assertIsNotNone(
            owner_task,
            "no task giving {{ pbs_datastore_mount }} to owner 'backup'; the "
            "PBS proxy cannot create vm/<vmid>/ under a root-owned datastore",
        )
        self.assertEqual(owner_task["ansible.builtin.file"].get("group"), "backup")

    def test_chown_happens_after_the_mount(self) -> None:
        """Ownership of a mount point is masked by the filesystem mounted over it."""
        mount = self._index(lambda t: "ansible.posix.mount" in t)
        chown = self._index(
            lambda t: "ansible.builtin.file" in t
            and t["ansible.builtin.file"].get("owner") == "backup"
            and t["ansible.builtin.file"].get("path") == "{{ pbs_datastore_mount }}"
        )
        create = self._index(
            lambda t: isinstance(t.get("ansible.builtin.command"), dict)
            and {"datastore", "create"} <= set(
                str(a) for a in t["ansible.builtin.command"].get("argv", []))
        )
        self.assertNotEqual(mount, -1, "no mount task")
        self.assertNotEqual(chown, -1, "no chown-to-backup task")
        self.assertLess(
            mount, chown,
            f"mount is task {mount} but the chown is task {chown}; chowning a "
            "mount point before mounting sets ownership on the hidden inode",
        )
        self.assertLess(
            chown, create,
            "the datastore root must belong to `backup` before PBS is pointed "
            "at it",
        )


class PbsTokenPrivileges(unittest.TestCase):
    """A PBS token's privileges are the INTERSECTION of its own and its user's.

    Granting only the token yields an empty permission set, the datastore API
    returns {"data":[]} instead of a 403, and `pvesm add pbs` then reports
    "Cannot find datastore 'lab', check permissions and existence!" while the
    token still authenticates with HTTP 200.
    """

    def setUp(self) -> None:
        self.tasks = _tasks()
        self.defaults = yaml.safe_load(DEFAULTS.read_text())

    def test_roles_are_granted_to_both_the_user_and_the_token(self) -> None:
        acl = next(
            (t for t in self.tasks
             if isinstance(t.get("ansible.builtin.command"), dict)
             and "acl" in [str(a) for a in
                           t["ansible.builtin.command"].get("argv", [])]),
            None,
        )
        self.assertIsNotNone(acl, "no acl update task")
        loop = str(acl.get("loop", ""))
        for needed in ("pbs_pve_user", "pbs_pve_token_authid"):
            with self.subTest(authid=needed):
                self.assertIn(
                    needed, loop,
                    f"the acl loop does not cover {needed}; granting only one "
                    "side of the intersection resolves to no privileges",
                )

    def test_audit_role_is_requested_not_just_backup(self) -> None:
        roles = self.defaults.get("pbs_pve_roles", [])
        self.assertIn("DatastoreBackup", roles)
        self.assertIn(
            "DatastoreAudit", roles,
            "pvesm add validates by enumerating the datastore, which "
            "DatastoreBackup alone does not permit",
        )

    def test_effective_privileges_are_asserted(self) -> None:
        """acl update returns 0 even when the resulting permission set is empty."""
        guard = next(
            (t for t in self.tasks
             if "ansible.builtin.assert" in t
             and "Datastore.Backup" in yaml.safe_dump(t["ansible.builtin.assert"])),
            None,
        )
        self.assertIsNotNone(
            guard,
            "role never asserts the token resolves Datastore.Backup; the acl "
            "commands succeed regardless, which is how the intersection "
            "behaviour stayed hidden",
        )
