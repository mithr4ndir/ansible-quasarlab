"""Regression tests for the kernel.threads-max task in tasks/main.yml.

Run from the repo root with the pinned runner:
    uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" \
        pytest roles/common/vm_baseline/tests

Why this exists: Proxmox memory hotplug boots these VMs with 1 GiB of static
RAM and hot-adds the rest, and the kernel sizes kernel.threads-max once, from
what it saw at boot. A 16 GiB VM therefore runs with the thread ceiling of a
1 GiB one, and every systemd percentage derived from it lands too low, which is
what killed herdr.service on 2026-09-21.

The expected values below are measured, not derived: threads-max as read on
hosts that boot WITHOUT memory hotplug, which is what a correctly sized ceiling
looks like. The task must reach those values and must never lower one.
"""

from __future__ import annotations

import base64
import unittest
from pathlib import Path

import jinja2
import yaml


ROLE = Path(__file__).resolve().parent.parent
REPO = ROLE.parent.parent.parent
DEFAULTS = ROLE / "defaults" / "main.yml"
TASKS = ROLE / "tasks" / "main.yml"
TASK_NAME = "Restore kernel.threads-max to what the running RAM warrants"

# (hostname, ansible_memtotal_mb, kernel.threads-max as booted)
#
# Read on 2026-09-22 and 2026-09-27. pbs1 and npm have no `memory` in their
# hotplug string, so their kernel saw all of their RAM and picked the ceiling
# this task is meant to reproduce. command-center1 and k8cluster1 do, and show
# what the bug looks like: a quarter of pbs1's ceiling with four times its RAM.
CORRECTLY_BOOTED = [
    ("pbs1", 3927, 31240),
    ("npm", 5925, 47094),
]
HOTPLUG_BOOTED = [
    ("command-center1", 16254, 6847),
    ("k8cluster1", 16254, 6848),
]


def _threads_max_for(memtotal_mb: int) -> int:
    expression = yaml.safe_load(DEFAULTS.read_text())["vm_baseline_threads_max"]
    rendered = (
        jinja2.Environment(undefined=jinja2.StrictUndefined)
        .from_string(expression)
        .render(ansible_memtotal_mb=memtotal_mb)
    )
    return int(rendered)


def _task() -> dict:
    for task in yaml.safe_load(TASKS.read_text()):
        if task.get("name") == TASK_NAME:
            return task
    raise AssertionError(f"task {TASK_NAME!r} not found in {TASKS}")


class ThreadsMaxValueTest(unittest.TestCase):
    def test_matches_what_a_host_without_memory_hotplug_gets(self) -> None:
        for host, memtotal_mb, measured in CORRECTLY_BOOTED:
            with self.subTest(host=host):
                computed = _threads_max_for(memtotal_mb)
                self.assertGreaterEqual(
                    computed, measured, "would lower a correctly sized ceiling"
                )
                self.assertLess(
                    computed,
                    measured * 1.02,
                    "drifted away from the kernel's own mempages / 32",
                )

    def test_repairs_the_hotplug_booted_hosts(self) -> None:
        for host, memtotal_mb, measured in HOTPLUG_BOOTED:
            with self.subTest(host=host):
                # The 1 GiB ceiling has to be lifted by more than an order of
                # magnitude, well clear of systemd's DefaultTasksMax=15% of it.
                self.assertGreater(_threads_max_for(memtotal_mb), measured * 10)

    def test_scales_with_ram_rather_than_being_a_constant(self) -> None:
        self.assertEqual(_threads_max_for(8192) * 2, _threads_max_for(16384))


class ThreadsMaxTaskTest(unittest.TestCase):
    def test_task_writes_and_applies_the_value(self) -> None:
        args = _task()["ansible.posix.sysctl"]
        self.assertEqual(args["name"], "kernel.threads-max")
        self.assertIn("vm_baseline_threads_max", args["value"])
        # sysctl_set applies it to the running kernel; state: present persists
        # it, so a reboot does not hand the ceiling back to the 1 GiB value.
        self.assertTrue(args["sysctl_set"])
        self.assertEqual(args["state"], "present")

    def test_not_folded_into_the_static_sysctl_dict(self) -> None:
        # vm_baseline_sysctl is a fleet-wide constant map; this value is
        # per host, and a constant here would be wrong on every other size.
        self.assertNotIn(
            "kernel.threads-max",
            yaml.safe_load(DEFAULTS.read_text())["vm_baseline_sysctl"],
        )


class NeverLowersTest(unittest.TestCase):
    """"Only ever raises" covers all three sources, not just two.

    Rendered through Ansible's own Templar: a folded YAML scalar hands Jinja the
    characters as written, so a doubled backslash in the regex silently matches
    nothing and the persisted term contributes 0 while still returning a
    plausible number. Bare Jinja2 cannot show that, and neither can reading it.
    """

    def setUp(self) -> None:
        try:
            from ansible.parsing.dataloader import DataLoader  # noqa: F401
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("ansible-core is not importable")
        self.expression = _task()["ansible.posix.sysctl"]["value"]

    def _render(self, computed: int, live: int, sysctl_conf: str) -> int:
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar

        variables = {
            "vm_baseline_threads_max": computed,
            "vm_baseline_threads_max_live": {
                "content": base64.b64encode(f"{live}\n".encode()).decode()
            },
            "vm_baseline_sysctl_conf": {
                "content": base64.b64encode(sysctl_conf.encode()).decode()
            },
        }
        return int(
            Templar(loader=DataLoader(), variables=variables).template(self.expression)
        )

    def test_cases(self) -> None:
        none = "net.ipv4.ip_forward=1\n"
        low = "kernel.threads-max=6847\n"
        high = "kernel.threads-max = 200000\n"
        for name, computed, live, conf, expected in (
            ("hotplug-booted host", 130032, 6847, none, 130032),
            ("steady state", 130032, 130032, low, 130032),
            ("persisted above computed and live", 130032, 130000, high, 200000),
            ("live above computed", 130032, 500000, low, 500000),
            ("a low override cannot lower", 8000, 10000, none, 10000),
            ("no sysctl.conf content", 130032, 6847, "", 130032),
        ):
            with self.subTest(name):
                self.assertEqual(self._render(computed, live, conf), expected)

    def test_reads_both_sources(self) -> None:
        sources = {
            t["ansible.builtin.slurp"]["src"]
            for t in yaml.safe_load(TASKS.read_text())
            if "ansible.builtin.slurp" in t
        }
        self.assertIn("/proc/sys/kernel/threads-max", sources)
        self.assertIn("/etc/sysctl.conf", sources)

    def test_matches_the_copy_in_cmd_center(self) -> None:
        """The same expression lives in two roles; it must not drift.

        cmd_center needs its own copy because playbooks/cmd_center.yml can run
        without vm_baseline, which the disaster-recovery path does deliberately.
        """
        cmd_center = (
            REPO / "roles/cmd_center/tasks/task_limits.yml"
        )
        other = [
            t
            for t in yaml.safe_load(cmd_center.read_text())
            if t.get("name", "").startswith("Choose the ceiling")
        ][0]["ansible.builtin.set_fact"]["cmd_center_threads_max_target"]

        def normalise(expression: str) -> str:
            expression = " ".join(expression.split())
            for ours, theirs in (
                ("vm_baseline_threads_max_live", "cmd_center_threads_max_before"),
                ("vm_baseline_sysctl_conf", "cmd_center_sysctl_conf"),
                ("vm_baseline_threads_max", "cmd_center_kernel_threads_max"),
            ):
                expression = expression.replace(ours, "X").replace(theirs, "X")
            return expression

        self.assertEqual(normalise(self.expression), normalise(other))


if __name__ == "__main__":
    unittest.main()
