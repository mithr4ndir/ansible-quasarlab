"""Regression tests for files/herdr-health-collector.sh.

Run from the repo root:
    python3 -m unittest discover roles/cmd_center/tests

A fake `systemctl` is put first on PATH, so no real user manager is
touched and the tests run anywhere bash is available.

HerdrRemoteLinkTests cover the /usr/local/bin/herdr link in tasks/herdr.yml
and its removal in tasks/main.yml. They do not run a playbook: the task
arguments are read from the YAML, rendered against temporary paths, and handed
to the real ansible.builtin.file and ansible.builtin.stat modules, and the
`when` conditions go through Ansible's own conditional evaluator. Skipped when
the system python has no ansible.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROLE = Path(__file__).resolve().parent.parent
COLLECTOR = ROLE / "files" / "herdr-health-collector.sh"
SYSTEM_PYTHON = "/usr/bin/python3"
LINK_TASK = "Link herdr into the non-login SSH PATH for herdr --remote"
REMOVE_BLOCK = "Remove the herdr --remote link when herdr is disabled"
STATES = ("activating", "active", "deactivating", "failed", "inactive")
SAMPLE = re.compile(r"^[a-z_]+(\{[^}]*\})? -?[0-9]+$")


class CollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.textfile_dir = self.root / "textfiles"
        self.textfile_dir.mkdir()
        self.prom = self.textfile_dir / "herdr.prom"
        # logger would write to the real journal; stub it out.
        self._write_exe("logger", "#!/bin/sh\nexit 0\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_exe(self, name: str, body: str) -> None:
        path = self.bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _fake_systemctl(self, stdout: str, exit_code: int) -> None:
        # Records its argv so the tests can check exactly what was asked.
        self._write_exe(
            "systemctl",
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" > "{self.root}/systemctl.args"\n'
            f"printf '%s' '{stdout}'\n"
            f"exit {exit_code}\n",
        )

    def _run(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "HERDR_HEALTH_PROM_FILE": str(self.prom),
        }
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(COLLECTOR)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _samples(self) -> dict[str, str]:
        lines = [l for l in self.prom.read_text().splitlines() if not l.startswith("#")]
        for line in lines:
            self.assertRegex(line, SAMPLE)
        return dict(line.rsplit(" ", 1) for line in lines)

    def _state_values(self, samples: dict[str, str]) -> dict[str, str]:
        return {
            s: samples[f'herdr_systemd_unit_state{{name="herdr.service",state="{s}"}}']
            for s in STATES
        }

    def test_active_unit(self) -> None:
        self._fake_systemctl("active", 0)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        samples = self._samples()
        values = self._state_values(samples)
        self.assertEqual(values.pop("active"), "1")
        self.assertEqual(set(values.values()), {"0"})
        self.assertEqual(samples["herdr_health_collector_success"], "1")
        self.assertGreater(int(samples["herdr_health_collector_timestamp_seconds"]), 0)
        self.assertEqual(
            (self.root / "systemctl.args").read_text().strip(),
            "--user is-active herdr.service",
        )

    def test_failed_unit_nonzero_exit_is_not_a_collector_error(self) -> None:
        # is-active exits 3 for anything but active; that must not count as failure.
        self._fake_systemctl("failed", 3)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        samples = self._samples()
        values = self._state_values(samples)
        self.assertEqual(values.pop("failed"), "1")
        self.assertEqual(set(values.values()), {"0"})
        self.assertEqual(samples["herdr_health_collector_success"], "1")

    def test_recognised_state_outside_the_broken_out_set(self) -> None:
        self._fake_systemctl("reloading", 0)
        self.assertEqual(self._run().returncode, 0)
        samples = self._samples()
        self.assertEqual(set(self._state_values(samples).values()), {"0"})
        self.assertEqual(samples["herdr_health_collector_success"], "1")

    def test_unreachable_user_manager(self) -> None:
        self._fake_systemctl("", 1)
        self.assertEqual(self._run().returncode, 0)
        samples = self._samples()
        self.assertEqual(set(self._state_values(samples).values()), {"0"})
        self.assertEqual(samples["herdr_health_collector_success"], "0")

    def test_hostile_state_output_is_never_echoed(self) -> None:
        self._fake_systemctl('active"} 1\nevil_metric 1', 0)
        self.assertEqual(self._run().returncode, 0)
        text = self.prom.read_text()
        self.assertNotIn("evil_metric", text)
        samples = self._samples()
        self.assertEqual(samples["herdr_health_collector_success"], "0")

    def test_invalid_unit_name_is_rejected(self) -> None:
        self._fake_systemctl("active", 0)
        proc = self._run(HERDR_HEALTH_UNIT='herdr.service",x="y')
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.prom.exists())

    def test_output_is_world_readable_and_no_temp_files_left(self) -> None:
        self._fake_systemctl("active", 0)
        self.assertEqual(self._run().returncode, 0)
        self.assertEqual(stat.S_IMODE(self.prom.stat().st_mode), 0o644)
        self.assertEqual(sorted(p.name for p in self.textfile_dir.iterdir()), ["herdr.prom"])

    def test_unwritable_directory_fails_loudly(self) -> None:
        self._fake_systemctl("active", 0)
        proc = self._run(HERDR_HEALTH_PROM_FILE=str(self.root / "missing" / "herdr.prom"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.root / "missing").exists())

    def test_rerun_replaces_previous_file(self) -> None:
        self._fake_systemctl("active", 0)
        self._run()
        self._fake_systemctl("inactive", 3)
        self._run()
        values = self._state_values(self._samples())
        self.assertEqual(values["inactive"], "1")
        self.assertEqual(values["active"], "0")

    # --- publish failures (#154) ------------------------------------------
    #
    # Each test publishes a good file first, then breaks one step of the next
    # run. The good file must survive byte for byte, no temp file may be left
    # behind, and the run must exit non-zero so systemd records the failure.

    def _publish_good_file(self) -> bytes:
        self._fake_systemctl("active", 0)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._fake_systemctl("inactive", 3)
        return self.prom.read_bytes()

    def _fake_mktemp_to_dev_full(self) -> None:
        # A real mktemp result, except the temp path is a symlink to /dev/full,
        # so every write through it fails with ENOSPC the way a full
        # filesystem would.
        self._write_exe(
            "mktemp",
            "#!/bin/sh\n"
            "p=\"$(printf '%s' \"$1\" | sed 's/XXXXXX$/devfull/')\"\n"
            'ln -s /dev/full "$p" || exit 1\n'
            'printf "%s\\n" "$p"\n',
        )

    def _assert_previous_file_untouched(self, proc, previous: bytes) -> None:
        self.assertNotEqual(proc.returncode, 0, "a failed publish must exit non-zero")
        self.assertFalse(self.prom.is_symlink())
        self.assertEqual(self.prom.read_bytes(), previous)
        self.assertEqual(stat.S_IMODE(self.prom.stat().st_mode), 0o644)
        self.assertEqual(sorted(p.name for p in self.textfile_dir.iterdir()), ["herdr.prom"])

    def test_failed_metric_write_keeps_previous_file(self) -> None:
        previous = self._publish_good_file()
        self._fake_mktemp_to_dev_full()
        # chmod succeeds, so only the write fails.
        self._write_exe("chmod", "#!/bin/sh\nexit 0\n")
        proc = self._run()
        self._assert_previous_file_untouched(proc, previous)

    def test_failed_metric_write_on_first_run_publishes_nothing(self) -> None:
        self._fake_systemctl("active", 0)
        self._fake_mktemp_to_dev_full()
        self._write_exe("chmod", "#!/bin/sh\nexit 0\n")
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(list(self.textfile_dir.iterdir()), [])

    def test_failed_chmod_keeps_previous_file(self) -> None:
        previous = self._publish_good_file()
        self._write_exe("chmod", "#!/bin/sh\nexit 1\n")
        proc = self._run()
        self._assert_previous_file_untouched(proc, previous)

    def test_failed_timestamp_keeps_previous_file(self) -> None:
        # An empty timestamp would publish an unparseable sample line.
        previous = self._publish_good_file()
        self._write_exe("date", "#!/bin/sh\nexit 1\n")
        proc = self._run()
        self._assert_previous_file_untouched(proc, previous)

    def test_failed_publish_is_logged(self) -> None:
        previous = self._publish_good_file()
        self._write_exe("chmod", "#!/bin/sh\nexit 1\n")
        self._write_exe("logger", f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.root}/logger.log"\n')
        proc = self._run()
        self._assert_previous_file_untouched(proc, previous)
        self.assertIn("reason=chmod_failed", (self.root / "logger.log").read_text())


def _ansible_available() -> bool:
    if not Path(SYSTEM_PYTHON).exists():
        return False
    return subprocess.run([SYSTEM_PYTHON, "-c", "import ansible.modules.file"],
                          capture_output=True, check=False).returncode == 0


# Evaluates a `when` list with Ansible's own Conditional, reading JSON on stdin.
EVALUATE_WHEN = r"""
import json, sys
from ansible.parsing.dataloader import DataLoader
from ansible.playbook.conditional import Conditional
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar
init_plugin_loader()
data = json.load(sys.stdin)
cond = Conditional(loader=DataLoader())
cond.when = data["when"]
print(json.dumps(cond.evaluate_conditional(Templar(loader=DataLoader()), data["vars"])))
"""


@unittest.skipUnless(_ansible_available(), "system python has no ansible")
class HerdrRemoteLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.herdr_tasks = yaml.safe_load((ROLE / "tasks" / "herdr.yml").read_text())
        cls.main_tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        cls.link_index, cls.link_task = next(
            (i, t) for i, t in enumerate(cls.herdr_tasks) if t["name"] == LINK_TASK)
        cls.remove_block = next(t for t in cls.main_tasks if t["name"] == REMOVE_BLOCK)
        cls.herdr_import = next(t for t in cls.main_tasks
                                if t.get("ansible.builtin.import_tasks") == "herdr.yml")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bin_path = self.root / "home" / ".local" / "bin" / "herdr"
        self.bin_path.parent.mkdir(parents=True)
        self.bin_path.write_text("#!/bin/sh\necho herdr 0.9.0\n")
        self.bin_path.chmod(0o755)
        self.link_path = self.root / "usr-local-bin" / "herdr"
        self.link_path.parent.mkdir()
        (self.root / "ansible.cfg").write_text("[defaults]\n")
        self.vars = {
            "cmd_center_herdr_bin_path": str(self.bin_path),
            "cmd_center_herdr_link_path": str(self.link_path),
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ansible_env(self) -> dict[str, str]:
        # An empty config, so the repo ansible.cfg (vault password script,
        # inventory) is never read.
        return {"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                "ANSIBLE_CONFIG": str(self.root / "ansible.cfg"),
                "ANSIBLE_LOCAL_TEMP": str(self.root / "ansible-tmp")}

    def _render(self, args: dict) -> dict:
        rendered = {}
        for key, value in args.items():
            if isinstance(value, str):
                for name, real in self.vars.items():
                    value = value.replace("{{ " + name + " }}", real)
                self.assertNotIn("{{", value, f"unrendered template in {key}")
            rendered[key] = value
        return rendered

    def _module(self, name: str, args: dict) -> dict:
        args_file = self.root / f"{name}-args.json"
        args_file.write_text(json.dumps({"ANSIBLE_MODULE_ARGS": self._render(args)}))
        proc = subprocess.run([SYSTEM_PYTHON, "-m", f"ansible.modules.{name}", str(args_file)],
                              env=self._ansible_env(), capture_output=True, text=True,
                              check=False, cwd=self.root)
        result = json.loads(proc.stdout)
        result["rc"] = proc.returncode
        return result

    def _when(self, task: dict, extra_vars: dict) -> bool:
        when = task.get("when", [])
        when = when if isinstance(when, list) else [when]
        proc = subprocess.run([SYSTEM_PYTHON, "-c", EVALUATE_WHEN],
                              input=json.dumps({"when": when, "vars": {**self.vars, **extra_vars}}),
                              env=self._ansible_env(), capture_output=True, text=True,
                              check=False, cwd=self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def _apply_removal(self, enabled: object) -> None:
        """Run main.yml's removal block the way Ansible would."""
        if not self._when(self.remove_block, {"cmd_center_herdr_enabled": enabled}):
            return
        stat_task, remove_task = self.remove_block["block"]
        stat = self._module("stat", stat_task["ansible.builtin.stat"])
        self.assertEqual(stat["rc"], 0, stat)
        variables = {"cmd_center_herdr_enabled": enabled,
                     stat_task["register"]: {"stat": stat["stat"]}}
        if self._when(remove_task, variables):
            self.assertEqual(self._module("file", remove_task["ansible.builtin.file"])["rc"], 0)

    # --- enabled -----------------------------------------------------------

    def test_enabled_creates_link_to_the_managed_binary(self) -> None:
        result = self._module("file", self.link_task["ansible.builtin.file"])
        self.assertEqual(result["rc"], 0, result)
        self.assertTrue(result["changed"])
        self.assertTrue(self.link_path.is_symlink())
        self.assertEqual(os.readlink(self.link_path), str(self.bin_path))
        # A link, never a second copy of the binary.
        self.assertTrue(self.link_path.samefile(self.bin_path))

    def test_rerun_over_existing_link_is_a_no_op(self) -> None:
        # Same shape as the link made by hand on command-center1.
        self.link_path.symlink_to(self.bin_path)
        result = self._module("file", self.link_task["ansible.builtin.file"])
        self.assertEqual(result["rc"], 0, result)
        self.assertFalse(result["changed"])
        self.assertEqual(os.readlink(self.link_path), str(self.bin_path))

    def test_refuses_to_replace_a_real_file(self) -> None:
        self.link_path.write_text("someone else's herdr")
        result = self._module("file", self.link_task["ansible.builtin.file"])
        self.assertNotEqual(result["rc"], 0)
        self.assertFalse(self.link_path.is_symlink())
        self.assertEqual(self.link_path.read_text(), "someone else's herdr")

    def test_link_task_shape_and_order(self) -> None:
        args = self.link_task["ansible.builtin.file"]
        self.assertEqual(args["state"], "link")
        self.assertEqual(args["src"], "{{ cmd_center_herdr_bin_path }}")
        self.assertEqual(args["dest"], "{{ cmd_center_herdr_link_path }}")
        self.assertIs(args["follow"], False)
        self.assertNotIn("force", args)
        self.assertIs(self.link_task["become"], True)
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertEqual(defaults["cmd_center_herdr_link_path"], "/usr/local/bin/herdr")
        names = [t["name"] for t in self.herdr_tasks]
        self.assertGreater(self.link_index, names.index("Install pinned herdr binary"))
        self.assertGreater(self.link_index, names.index("Assert installed herdr version matches the pin"))
        self.assertLess(self.link_index, names.index("Enable herdr user service"))
        # Nothing copies the binary to the link path.
        for task in self.herdr_tasks:
            for module in ("ansible.builtin.copy", "ansible.builtin.get_url"):
                if module in task:
                    self.assertNotIn("link_path", str(task[module].get("dest", "")))

    # --- disabled ----------------------------------------------------------

    def test_herdr_tasks_and_removal_are_mutually_exclusive(self) -> None:
        for enabled in (True, False, "true", "false", "yes", "no"):
            with self.subTest(enabled=enabled):
                runs_herdr = self._when(self.herdr_import, {"cmd_center_herdr_enabled": enabled})
                runs_removal = self._when(self.remove_block, {"cmd_center_herdr_enabled": enabled})
                self.assertNotEqual(runs_herdr, runs_removal)

    def test_disabled_removes_the_link(self) -> None:
        self.link_path.symlink_to(self.bin_path)
        self._apply_removal(enabled=False)
        self.assertFalse(self.link_path.is_symlink())
        self.assertFalse(self.link_path.exists())
        self.assertTrue(self.bin_path.exists())

    def test_disabled_removes_a_dangling_link(self) -> None:
        self.link_path.symlink_to(self.bin_path)
        self.bin_path.unlink()
        self._apply_removal(enabled=False)
        self.assertFalse(self.link_path.is_symlink())

    def test_disabled_with_no_link_is_a_no_op(self) -> None:
        self._apply_removal(enabled=False)
        self.assertFalse(self.link_path.is_symlink())

    def test_enabled_leaves_the_link(self) -> None:
        self.link_path.symlink_to(self.bin_path)
        self._apply_removal(enabled=True)
        self.assertTrue(self.link_path.is_symlink())

    def test_disabled_leaves_files_it_does_not_own(self) -> None:
        self.link_path.write_text("someone else's herdr")
        self._apply_removal(enabled=False)
        self.assertEqual(self.link_path.read_text(), "someone else's herdr")
        self.link_path.unlink()
        other = self.root / "other-herdr"
        other.write_text("x")
        self.link_path.symlink_to(other)
        self._apply_removal(enabled=False)
        self.assertTrue(self.link_path.is_symlink())


if __name__ == "__main__":
    unittest.main()
