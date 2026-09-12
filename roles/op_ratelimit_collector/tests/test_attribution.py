"""Tests for op_ratelimit_collector attribution.py.

Run from the role directory:
    python3 -m unittest discover tests

Or from the repo root:
    python3 -m unittest discover roles/op_ratelimit_collector/tests
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures"
SCRIPT = Path(__file__).parent.parent / "files" / "attribution.py"

_spec = importlib.util.spec_from_file_location("attribution", SCRIPT)
attribution = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(attribution)


def event(**overrides: object) -> str:
    ev = {"ts": 1789200000, "pid": 1, "ppid": 1, "unit": "ansible-security.service",
          "consumer": "run-security", "caller": "run-security", "chain": "", "parent": "",
          "subcommand": "read", "slug": "wazuh_password", "ref": ""}
    ev.update(overrides)
    return json.dumps(ev, separators=(",", ":")) + "\n"


class AttributionSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="op-attr-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / "op-invocations.log"
        self.rotated = self.tmp / "op-invocations.log.1"
        self.state = self.tmp / "state.json"
        self.out = self.tmp / "op_invocations.prom"

    def collect(self, max_bytes: int = 1 << 20) -> int:
        return attribution.main([
            "--log", str(self.log), "--state", str(self.state),
            "--out", str(self.out), "--max-bytes", str(max_bytes),
        ])

    def append(self, path: Path, text: str) -> None:
        with open(path, "a") as fh:
            fh.write(text)

    def total(self, **labels: str) -> int:
        """Sum of onepassword_op_invocations_total series matching labels."""
        total = 0
        for line in self.out.read_text().splitlines():
            if not line.startswith("onepassword_op_invocations_total{"):
                continue
            if all(f'{k}="{v}"' in line for k, v in labels.items()):
                total += int(line.rsplit(" ", 1)[1])
        return total


class FixtureTests(AttributionSandbox):
    def test_sample_log_renders_expected_textfile(self) -> None:
        shutil.copy(FIXTURES / "shim_sample.log", self.log)
        self.assertEqual(self.collect(), 0)
        self.assertEqual(self.out.read_text(), (FIXTURES / "shim_sample.expected.prom").read_text())

    def test_cli_entrypoint(self) -> None:
        shutil.copy(FIXTURES / "shim_sample.log", self.log)
        proc = subprocess.run(
            ["python3", str(SCRIPT), "--log", str(self.log), "--state", str(self.state),
             "--out", str(self.out)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_missing_log_writes_empty_counters(self) -> None:
        self.assertEqual(self.collect(), 0)
        text = self.out.read_text()
        self.assertIn("# TYPE onepassword_op_invocations_total counter", text)
        self.assertIn("onepassword_op_shim_malformed_lines_total 0", text)


class CounterTests(AttributionSandbox):
    def test_counts_accumulate_across_runs_without_recounting(self) -> None:
        self.append(self.log, event() * 3)
        self.collect()
        self.assertEqual(self.total(consumer="run-security"), 3)
        self.collect()
        self.assertEqual(self.total(consumer="run-security"), 3)
        self.append(self.log, event(consumer="run-proxmox") * 2)
        self.collect()
        self.assertEqual(self.total(consumer="run-security"), 3)
        self.assertEqual(self.total(consumer="run-proxmox"), 2)

    def test_partial_trailing_line_waits_for_completion(self) -> None:
        line = event()
        self.append(self.log, event() + line[:40])
        self.collect()
        self.assertEqual(self.total(), 1)
        self.assertIn("onepassword_op_shim_malformed_lines_total 0", self.out.read_text())
        self.append(self.log, line[40:])
        self.collect()
        self.assertEqual(self.total(), 2)

    def test_truncated_log_starts_over(self) -> None:
        self.append(self.log, event() * 5)
        self.collect()
        self.log.write_text(event())
        self.collect()
        self.assertEqual(self.total(), 6)

    def test_label_injection_is_neutralized(self) -> None:
        self.append(self.log, event(caller='a"} 99\nx{', subcommand="read\n", slug="s{}"))
        self.collect()
        text = self.out.read_text()
        self.assertEqual(self.total(caller="other", subcommand="other", slug="other"), 1)
        self.assertNotIn("99", text)
        for line in text.splitlines():
            self.assertRegex(line, r'^(#|onepassword_[a-z_]+(\{[^"\n]*("[^"\\\n]*"[^"\n]*)*\})? \d+$)')

    def test_non_object_json_is_malformed(self) -> None:
        self.append(self.log, "[1,2]\n42\n\n" + event())
        self.collect()
        self.assertEqual(self.total(), 1)
        self.assertIn("onepassword_op_shim_malformed_lines_total 2", self.out.read_text())

    def test_corrupt_state_starts_fresh(self) -> None:
        self.append(self.log, event())
        self.state.write_text("{not json")
        self.assertEqual(self.collect(), 0)
        self.assertEqual(self.total(), 1)

    def test_unit_normalization(self) -> None:
        cases = {
            "ansible-proxmox.service": "ansible-proxmox",
            "session-42.scope": "session",
            "user@1000.service": "user",
            "docker-abc.scope": "scope",
            "": "unknown",
            None: "unknown",
            "init.scope/weird": "other",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(attribution.normalize_unit(raw), expected)


class RotationTests(AttributionSandbox):
    def test_rotation_neither_loses_nor_double_counts(self) -> None:
        line = event()
        self.append(self.log, line * 10)
        self.collect(max_bytes=len(line) * 5)
        self.assertEqual(self.total(), 10)
        self.assertTrue(self.rotated.exists())
        self.assertEqual(self.log.read_text(), "")

        # A shim that opened the log just before the rename lands its line in
        # the rotated file after the collector read it.
        self.append(self.rotated, event(consumer="late-writer"))
        self.append(self.log, event(consumer="after-rotation") * 2)
        self.collect(max_bytes=len(line) * 5)
        self.assertEqual(self.total(consumer="late-writer"), 1)
        self.assertEqual(self.total(consumer="after-rotation"), 2)
        self.assertEqual(self.total(), 13)

        # Steady state: nothing new, nothing recounted.
        self.collect(max_bytes=len(line) * 5)
        self.assertEqual(self.total(), 13)

    def test_second_rotation_replaces_first(self) -> None:
        line = event()
        for _ in range(3):
            self.append(self.log, line * 6)
            self.collect(max_bytes=len(line) * 5)
        self.assertEqual(self.total(), 18)
        state = json.loads(self.state.read_text())
        self.assertLessEqual(len(state["offsets"]), 2)

    def test_unknown_rotated_file_is_ignored(self) -> None:
        self.append(self.rotated, event() * 4)
        self.append(self.log, event())
        self.collect()
        self.assertEqual(self.total(), 1)


if __name__ == "__main__":
    unittest.main()
