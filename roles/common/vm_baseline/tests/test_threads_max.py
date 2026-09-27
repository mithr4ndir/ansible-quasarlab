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

import unittest
from pathlib import Path

import jinja2
import yaml


ROLE = Path(__file__).resolve().parent.parent
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
    """"Only ever raises" is enforced, not just intended.

    A vm_baseline_threads_max override, or a host whose reported memory shrank,
    would otherwise write a value below the ceiling already in force.
    """

    def _value_expression(self) -> str:
        return _task()["ansible.posix.sysctl"]["value"]

    def _render_with(self, computed: int, live: int) -> int:
        expression = self._value_expression().replace(
            "vm_baseline_threads_max_live.content | b64decode | trim | int", str(live)
        )
        rendered = (
            jinja2.Environment(undefined=jinja2.StrictUndefined)
            .from_string(expression)
            .render(vm_baseline_threads_max=computed)
        )
        return int(rendered)

    def test_raises_a_hotplug_booted_ceiling(self) -> None:
        self.assertEqual(self._render_with(130032, 6847), 130032)

    def test_keeps_a_higher_live_ceiling(self) -> None:
        self.assertEqual(self._render_with(130032, 200000), 200000)

    def test_reads_the_live_value_from_proc(self) -> None:
        slurps = [
            t
            for t in yaml.safe_load(TASKS.read_text())
            if "ansible.builtin.slurp" in t
        ]
        self.assertTrue(
            any(
                t["ansible.builtin.slurp"]["src"] == "/proc/sys/kernel/threads-max"
                for t in slurps
            ),
            "nothing reads the ceiling in force, so the max() cannot be honest",
        )


if __name__ == "__main__":
    unittest.main()
