"""Tests for the pinned, held kubectl install in tasks/main.yml.

Run from the repo root:
    python3 -m unittest discover roles/common/apps/kubectl/tests

Nothing here runs Ansible or apt. The held-package guard is a shell script, so
it is extracted from the task file verbatim and run against a fake apt-get
that replays simulation output. fixtures/apt_simulate_command_center1.txt is
the real `apt-get --simulate install kubectl=1.33.11-1.1
--allow-change-held-packages` output from command-center1 on 2026-09-13, with
kubectl held at 1.33.3-1.1 (the non-root "NOTE: This is only a simulation"
banner removed, since the task runs it as root).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROLE = Path(__file__).resolve().parent.parent
REPO = ROLE.parents[3]
TASKS = ROLE / "tasks" / "main.yml"
GROUP_VARS = REPO / "group_vars" / "all" / "main.yml"
SIMULATION = Path(__file__).resolve().parent / "fixtures" / "apt_simulate_command_center1.txt"


def load_tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def walk(tasks: list[dict]):
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from walk(task.get(key, []))


class PinnedVersionTests(unittest.TestCase):
    def test_kube_version_matches_the_cluster_exactly(self) -> None:
        kube_version = yaml.safe_load(GROUP_VARS.read_text())["kube_version"]
        self.assertEqual(kube_version, "1.33.11-1.1")
        self.assertRegex(kube_version, r"^\d+\.\d+\.\d+-\d+\.\d+$")

    def test_no_unpinned_state_anywhere_in_the_role(self) -> None:
        for task in walk(load_tasks()):
            apt = task.get("ansible.builtin.apt")
            if apt:
                self.assertNotEqual(apt.get("state"), "latest", task["name"])
                for name in apt["name"]:
                    self.assertIn("={{ kube_version }}", name, task["name"])


class HoldHandlingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = load_tasks()
        cls.guard_index, cls.guard = next(
            (i, t) for i, t in enumerate(cls.tasks) if "ansible.builtin.shell" in t
            and "apt-get --simulate" in t["ansible.builtin.shell"])
        cls.block_index, cls.block = next(
            (i, t) for i, t in enumerate(cls.tasks) if "block" in t)

    def test_install_allows_changing_the_hold_for_kubectl_only(self) -> None:
        installs = [t for t in walk(self.tasks) if "ansible.builtin.apt" in t]
        self.assertEqual(len(installs), 1)
        apt = installs[0]["ansible.builtin.apt"]
        self.assertEqual(apt["name"], ["kubectl={{ kube_version }}"])
        self.assertEqual(apt["state"], "present")
        self.assertIs(apt["allow_change_held_packages"], True)
        self.assertIn(installs[0], self.block["block"])

    def test_hold_is_reinstated_in_always(self) -> None:
        holds = [t["ansible.builtin.dpkg_selections"] for t in self.block.get("always", [])
                 if "ansible.builtin.dpkg_selections" in t]
        self.assertEqual(holds, [{"name": "kubectl", "selection": "hold"}])
        self.assertIs(self.block.get("become"), True)

    def test_kubeadm_and_kubelet_are_never_touched(self) -> None:
        text = TASKS.read_text()
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotRegex(code, r"kubeadm|kubelet")
        for task in walk(self.tasks):
            sel = task.get("ansible.builtin.dpkg_selections")
            if sel:
                self.assertEqual(sel["selection"], "hold")

    def test_guard_runs_before_install_and_under_check_mode(self) -> None:
        self.assertLess(self.guard_index, self.block_index)
        self.assertIs(self.guard.get("check_mode"), False)
        self.assertIs(self.guard.get("changed_when"), False)
        self.assertEqual(self.guard["args"]["executable"], "/bin/bash")
        self.assertEqual(self.guard["environment"], {"KUBE_VERSION": "{{ kube_version }}"})


class GuardScriptTests(unittest.TestCase):
    """Runs the guard's exact shell text against a fake apt-get."""

    @classmethod
    def setUpClass(cls) -> None:
        guard = next(t for t in load_tasks() if "ansible.builtin.shell" in t
                     and "apt-get --simulate" in t["ansible.builtin.shell"])
        cls.script = guard["ansible.builtin.shell"]
        cls.real_output = SIMULATION.read_text()

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kubectl-guard-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in ("awk", "bash", "cat"):
            found = shutil.which(tool)
            if found is None:
                self.skipTest(f"{tool} not installed")
            (self.bin / tool).symlink_to(found)
        self.argv_file = self.tmp / "apt-argv"

    def run_guard(self, apt_output: str, apt_rc: int = 0) -> subprocess.CompletedProcess:
        (self.tmp / "apt-output").write_text(apt_output)
        fake = self.bin / "apt-get"
        fake.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$@" > "{self.argv_file}"\n'
            f'cat "{self.tmp / "apt-output"}"\n'
            f"exit {apt_rc}\n"
        )
        fake.chmod(0o755)
        return subprocess.run(
            [str(self.bin / "bash"), "-c", self.script],
            env={"PATH": str(self.bin), "KUBE_VERSION": "1.33.11-1.1"},
            capture_output=True, text=True, timeout=30, check=False,
        )

    def test_real_command_center1_simulation_passes(self) -> None:
        self.assertIn("Inst kubectl [1.33.3-1.1] (1.33.11-1.1", self.real_output)
        proc = self.run_guard(self.real_output)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.argv_file.read_text().splitlines(),
                         ["--simulate", "install", "kubectl=1.33.11-1.1",
                          "--allow-change-held-packages"])

    def test_already_at_target_passes(self) -> None:
        output = ("kubectl is already the newest version (1.33.11-1.1).\n"
                  "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n")
        self.assertEqual(self.run_guard(output).returncode, 0)

    def test_arch_qualified_kubectl_passes(self) -> None:
        output = "Inst kubectl:amd64 [1.33.3-1.1] (1.33.11-1.1 pkgs.k8s.io [amd64])\n"
        self.assertEqual(self.run_guard(output).returncode, 0)

    def test_refuses_when_another_held_package_would_change(self) -> None:
        for extra in ("Inst kubeadm [1.33.3-1.1] (1.33.11-1.1 pkgs.k8s.io [amd64])",
                      "Remv kubelet [1.33.3-1.1]",
                      "Purg cri-tools [1.33.0-1.1]",
                      "Inst kubectl-convert (1.33.11-1.1 pkgs.k8s.io [amd64])"):
            with self.subTest(extra=extra):
                proc = self.run_guard(self.real_output + extra + "\n")
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(f"would change: {extra}", proc.stdout)

    def test_apt_failure_fails_the_guard(self) -> None:
        output = "E: Version '1.33.11-1.1' for 'kubectl' was not found\n"
        proc = self.run_guard(output, apt_rc=100)
        self.assertEqual(proc.returncode, 100)


if __name__ == "__main__":
    unittest.main()
