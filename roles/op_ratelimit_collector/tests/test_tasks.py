"""Tests for the shim install and removal conditions in tasks/main.yml.

Run from the repo root:
    python3 -m unittest discover roles/op_ratelimit_collector/tests

Nothing here runs Ansible. The two `when` conditions are evaluated in Python
for every combination of inputs, which is enough to prove the shim is never
left in place without a real op behind it.
"""

from __future__ import annotations

import itertools
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml


TASKS = Path(__file__).resolve().parent.parent / "tasks" / "main.yml"
# Only what these two conditions use. Anything else fails the guard below
# rather than being evaluated.
ALLOWED_NAMES = {"not", "or", "and", "_bool", "op_quota_shim_enabled", "op_quota_real_op_stat"}


def ansible_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"yes", "on", "1", "true", "y", "t"}


def evaluate_when(when: object, variables: dict) -> bool:
    conditions = when if isinstance(when, list) else [when]
    for condition in conditions:
        expr = re.sub(r"([\w.]+)\s*\|\s*bool", r"_bool(\1)", str(condition))
        if not re.fullmatch(r"[\w\s().]+", expr):
            raise AssertionError(f"unsupported condition syntax: {condition!r}")
        names = {tok.split(".")[0] for tok in re.findall(r"[A-Za-z_][\w.]*", expr)}
        if not names <= ALLOWED_NAMES:
            raise AssertionError(f"unexpected names {names - ALLOWED_NAMES} in {condition!r}")
        scope = {"_bool": ansible_bool, **variables}
        if not eval(expr, {"__builtins__": {}}, scope):  # noqa: S307 - guarded repo text
            return False
    return True


class ShimInstallConditionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        tasks = yaml.safe_load(TASKS.read_text())
        cls.stat_task = next(t for t in tasks if "ansible.builtin.stat" in t
                             and t["ansible.builtin.stat"]["path"] == "{{ op_quota_real_op }}")
        cls.install = next(t for t in tasks if t.get("ansible.builtin.copy", {}).get("src") == "op-shim")
        cls.remove = next(t for t in tasks if t.get("ansible.builtin.file", {}).get("path")
                          == "{{ op_quota_shim_path }}"
                          and t["ansible.builtin.file"].get("state") == "absent")

    def runs(self, task: dict, enabled: object, real_op_exists: bool) -> bool:
        variables = {
            "op_quota_shim_enabled": enabled,
            "op_quota_real_op_stat": SimpleNamespace(stat=SimpleNamespace(exists=real_op_exists)),
        }
        return evaluate_when(task.get("when", True), variables)

    def test_shim_removed_when_real_binary_gone(self) -> None:
        # A previous run installed the shim, then /usr/bin/op was removed.
        self.assertFalse(self.runs(self.install, True, False))
        self.assertTrue(self.runs(self.remove, True, False))

    def test_shim_installed_and_kept_when_enabled_with_real_binary(self) -> None:
        self.assertTrue(self.runs(self.install, True, True))
        self.assertFalse(self.runs(self.remove, True, True))

    def test_exactly_one_of_install_and_remove_runs(self) -> None:
        for enabled, exists in itertools.product([True, False, "true", "false", "yes", "no"],
                                                 [True, False]):
            with self.subTest(enabled=enabled, real_op_exists=exists):
                install = self.runs(self.install, enabled, exists)
                remove = self.runs(self.remove, enabled, exists)
                self.assertNotEqual(install, remove)
                self.assertEqual(install, ansible_bool(enabled) and exists)

    def test_dangling_symlink_counts_as_absent(self) -> None:
        # Without follow the stat is an lstat, and a dangling /usr/bin/op
        # symlink would report exists: true.
        self.assertIs(self.stat_task["ansible.builtin.stat"].get("follow"), True)


if __name__ == "__main__":
    unittest.main()
