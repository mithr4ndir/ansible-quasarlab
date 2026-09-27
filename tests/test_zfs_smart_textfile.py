"""A failing disk must still produce metrics.

`smartctl` reports a failed overall-health check by setting bit 3 of its exit
status, while still printing the health and attribute data. The exporter used to
guard the call with `|| continue`, so it skipped precisely the disks it exists to
catch, and its own `smart_device_health 0` branch was unreachable: the metric
could only ever be 1 or absent. A drive that silently stops being reported looks
identical to a healthy one on a dashboard, which matters here because the tank
pool has three drives actively dropping off the SATA bus.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "roles" / "truenas" / "files" / "zfs-smart-textfile.sh"

FAILING = """\
smartctl 7.4 2023-08-01 r5530 [x86_64-linux] (local build)

Device Model:     Inland IB24AK 1TB
Serial Number:    VE1R9204ABCD
SMART support is: Enabled

SMART overall-health self-assessment test result: FAILED!
Drive failure expected in less than 24 hours.

ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  9 Power_On_Hours          0x0032   099   099   000    Old_age   Always       -       4211
5 Reallocated_Sector_Ct     0x0033   001   001   036    Pre-fail  Always   FAILING_NOW  1288
194 Temperature_Celsius     0x0022   065   045   000    Old_age   Always       -       41
199 CRC_Error_Count         0x003e   200   200   000    Old_age   Always       -       7
"""

HEALTHY = FAILING.replace(
    "SMART overall-health self-assessment test result: FAILED!",
    "SMART overall-health self-assessment test result: PASSED",
).replace("Serial Number:    VE1R9204ABCD", "Serial Number:    VE1R9004WXYZ")


def samples(out: str, metric: str) -> list[str]:
    """Sample lines for a metric, ignoring `# HELP`/`# TYPE` headers.

    Asserting with `metric in out` is vacuous here: the script prints every
    HELP/TYPE header unconditionally before the device loop, so the bare name
    is present even when the disk was skipped entirely.
    """
    return [l for l in out.splitlines() if l.startswith(f"{metric}{{")]


class Harness:
    """Runs the real script against a stub smartctl and fake device nodes."""

    def __init__(self, tmp: str, stdout: str, exit_code: int):
        self.tmp = Path(tmp)
        bin_dir = self.tmp / "bin"
        dev_dir = self.tmp / "dev"
        self.out_dir = self.tmp / "out"
        for d in (bin_dir, dev_dir, self.out_dir):
            d.mkdir(parents=True, exist_ok=True)
        (dev_dir / "sda").touch()

        smartctl = bin_dir / "smartctl"
        smartctl.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                cat <<'SMARTOUT'
                {textwrap.indent(stdout, '                ').lstrip()}
                SMARTOUT
                exit {exit_code}
                """
            ).replace("\n                ", "\n")
        )
        smartctl.chmod(0o755)
        # zpool is absent on the test host; stub it so the pool loop is a no-op.
        zpool = bin_dir / "zpool"
        zpool.write_text("#!/bin/sh\nexit 0\n")
        zpool.chmod(0o755)

        self.env = dict(os.environ)
        self.env["PATH"] = f"{bin_dir}:{self.env['PATH']}"
        self.env["TEXTFILE_DIR"] = str(self.out_dir)
        self.env["SMART_DEV_GLOB"] = f"{dev_dir}/sd?"

    def run(self) -> str:
        proc = subprocess.run(
            ["bash", str(SCRIPT)], env=self.env, capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, f"script failed: {proc.stderr}"
        written = self.out_dir / "zfs_smart.prom"
        assert written.exists(), (
            f"nothing written to the configured TEXTFILE_DIR; stderr: {proc.stderr}"
        )
        return written.read_text()


class FailingDiskTests(unittest.TestCase):
    def test_failing_disk_reports_health_zero(self):
        """exit bit 3 set (8) plus real output: the disk must NOT be skipped."""
        with TemporaryDirectory() as tmp:
            out = Harness(tmp, FAILING, 8).run()
        health = samples(out, "smart_device_health")
        self.assertTrue(
            health,
            "the failing disk produced no health SAMPLE, so a dying drive is "
            "indistinguishable from a healthy one",
        )
        self.assertTrue(
            all(l.endswith(" 0") for l in health), f"expected health 0, got {health}"
        )

    def test_failing_disk_still_reports_attributes(self):
        with TemporaryDirectory() as tmp:
            out = Harness(tmp, FAILING, 8).run()
        for metric in (
            "smart_reallocated_sector_count",
            "smart_power_on_hours",
            "smart_crc_error_count",
        ):
            with self.subTest(metric=metric):
                self.assertTrue(
                    samples(out, metric),
                    f"{metric} has no sample line; the failing disk was skipped",
                )

    def test_exit_bitmask_is_exposed(self):
        with TemporaryDirectory() as tmp:
            out = Harness(tmp, FAILING, 8).run()
        got = samples(out, "smart_smartctl_exit_status")
        self.assertTrue(got, "raw smartctl bitmask not reported as a sample")
        self.assertTrue(
            all(l.endswith(" 8") for l in got),
            f"expected the bitmask 8 to be exposed, got {got}",
        )


class UnreachableDeviceTests(unittest.TestCase):
    def test_open_failure_is_reported_not_silent(self):
        """bit 1 (2) with no output: nothing to parse, but say so explicitly."""
        with TemporaryDirectory() as tmp:
            out = Harness(tmp, "", 2).run()
        ok = samples(out, "smart_collect_ok")
        self.assertTrue(ok, "an unreadable device emitted no collect_ok sample")
        self.assertTrue(
            all(l.endswith(" 0") for l in ok),
            f"a device we could not read must report collect_ok 0, got {ok}",
        )
        self.assertNotIn(
            "smart_device_health{", out,
            "must not invent a health value for a device that never answered",
        )


class HealthyDiskTests(unittest.TestCase):
    def test_healthy_disk_still_reports_one(self):
        """Guard against 'fixing' the bug by reporting 0 for everything."""
        with TemporaryDirectory() as tmp:
            out = Harness(tmp, HEALTHY, 0).run()
        health = samples(out, "smart_device_health")
        self.assertTrue(health, "healthy disk emitted no health sample")
        self.assertTrue(
            all(l.endswith(" 1") for l in health), f"expected health 1, got {health}"
        )


class ConfiguredDirectoryTests(unittest.TestCase):
    def test_script_honours_the_configured_textfile_dir(self):
        """The role's truenas_textfile_dir must reach the script, or the
        documented override produces no metrics."""
        with TemporaryDirectory() as tmp:
            h = Harness(tmp, HEALTHY, 0)
            h.run()
            self.assertTrue((h.out_dir / "zfs_smart.prom").exists())

    def test_service_unit_passes_the_configured_dir(self):
        unit = (REPO / "roles" / "truenas" / "templates"
                / "zfs-smart-textfile.service.j2").read_text()
        self.assertIn(
            "TEXTFILE_DIR={{ truenas_textfile_dir }}", unit,
            "the service must pass truenas_textfile_dir through, since the script "
            "is copied verbatim from files/ and cannot template it",
        )
