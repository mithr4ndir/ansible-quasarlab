"""Regression tests for files/herdr-health-collector.sh.

Run from the repo root:
    python3 -m unittest discover roles/cmd_center/tests

A fake `systemctl` is put first on PATH, so no real user manager is
touched and the tests run anywhere bash is available.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


COLLECTOR = Path(__file__).resolve().parent.parent / "files" / "herdr-health-collector.sh"
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


if __name__ == "__main__":
    unittest.main()
