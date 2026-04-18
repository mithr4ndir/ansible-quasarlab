"""Regression tests for op_quota_collector parse.py.

Run from the role directory:
    python3 -m unittest discover tests

Or from the repo root:
    python3 -m unittest discover roles/op_quota_collector/tests
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures"
PARSER = Path(__file__).parent.parent / "files" / "parse.py"


def run_parser(stdin_file: Path) -> tuple[int, str, str]:
    with open(stdin_file, "rb") as fh:
        proc = subprocess.run(
            ["python3", str(PARSER)],
            stdin=fh,
            capture_output=True,
            text=True,
            check=False,
        )
    return proc.returncode, proc.stdout, proc.stderr


class ParseTests(unittest.TestCase):
    def _assert_fixture(self, name: str) -> None:
        rc, out, err = run_parser(FIXTURES / f"{name}.txt")
        self.assertEqual(rc, 0, msg=f"parser failed: {err}")
        expected = (FIXTURES / f"{name}.expected.prom").read_text()
        self.assertEqual(out, expected)

    def test_clean_state(self) -> None:
        self._assert_fixture("clean")

    def test_exhausted_state(self) -> None:
        self._assert_fixture("exhausted")

    def test_all_na_resets(self) -> None:
        rc, out, err = run_parser(FIXTURES / "all_na.txt")
        self.assertEqual(rc, 0, msg=err)
        # When all RESET values are N/A, no reset_seconds lines should appear
        self.assertNotIn("onepassword_ratelimit_reset_seconds", out)
        # But used/limit/remaining should all be present
        self.assertIn("onepassword_ratelimit_used", out)
        self.assertIn("onepassword_ratelimit_limit", out)
        self.assertIn("onepassword_ratelimit_remaining", out)

    def test_malformed_header_exits_2(self) -> None:
        rc, out, err = run_parser(FIXTURES / "malformed_header.txt")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("error:", err)

    def test_empty_input_exits_2(self) -> None:
        proc = subprocess.run(
            ["python3", str(PARSER)],
            input="",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("empty input", proc.stderr)


if __name__ == "__main__":
    unittest.main()
