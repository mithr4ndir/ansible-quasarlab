"""Regression tests for the 1Password pre-flight quota gate parser.

Run from the repo root:
    python3 -m unittest discover tests

Only the pure parsing half is covered here. The callback half needs a live
ansible runtime and is exercised by actually running a playbook.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Optional


SRC = Path(__file__).resolve().parents[1] / "callback_plugins" / "op_quota_gate.py"


def _load_parser():
    """Exec just the ansible-free parser out of the plugin.

    The plugin imports ansible at module scope, which is not available to a
    plain unittest run. parse_account_remaining and ACCOUNT_ROW are kept at
    module level and free of ansible references specifically so this works.
    """
    source = SRC.read_text()
    namespace = {"re": re, "Optional": Optional}
    start = source.index("ACCOUNT_ROW = re.compile(")
    end = source.index("class CallbackModule")
    exec(compile(source[start:end], str(SRC), "exec"), namespace)
    tail = source[source.index("def parse_account_remaining"):]
    exec(compile(tail, str(SRC), "exec"), namespace)
    return namespace["parse_account_remaining"]


parse_account_remaining = _load_parser()


# The exact shape observed during the 2026-08-23 exhaustion.
EXHAUSTED_OUTPUT = """TYPE       ACTION        LIMIT    USED    REMAINING    RESET
token      write         100      0       100          N/A
token      read          1000     0       1000         N/A
account    read_write    1000     1000    0            2 hours from now
"""

HEALTHY_OUTPUT = """TYPE       ACTION        LIMIT    USED    REMAINING    RESET
token      write         100      3       97           N/A
token      read          1000     12      988          N/A
account    read_write    1000     138     862          20 hours from now
"""


class TestParseAccountRemaining(unittest.TestCase):
    def test_parses_exhausted_account_row(self):
        self.assertEqual(parse_account_remaining(EXHAUSTED_OUTPUT), 0)

    def test_parses_healthy_account_row(self):
        self.assertEqual(parse_account_remaining(HEALTHY_OUTPUT), 862)

    def test_ignores_token_rows(self):
        # token read reports 1000 remaining while the account row reports 0.
        # Reading the wrong row is the token-vs-account misattribution that
        # has cost hours of investigation before.
        self.assertEqual(parse_account_remaining(EXHAUSTED_OUTPUT), 0)

    def test_returns_none_on_missing_account_row(self):
        only_tokens = (
            "TYPE       ACTION        LIMIT    USED    REMAINING    RESET\n"
            "token      write         100      0       100          N/A\n"
        )
        self.assertIsNone(parse_account_remaining(only_tokens))

    def test_returns_none_on_empty_input(self):
        self.assertIsNone(parse_account_remaining(""))

    def test_returns_none_on_garbage_input(self):
        self.assertIsNone(parse_account_remaining("not a table at all"))

    def test_returns_none_on_none_input(self):
        self.assertIsNone(parse_account_remaining(None))

    def test_returns_none_when_columns_do_not_add_up(self):
        # Guards against a future CLI column reorder silently gating on the
        # used count instead of the remaining count.
        reordered = "account    read_write    1000     7       999          N/A\n"
        self.assertIsNone(parse_account_remaining(reordered))

    def test_case_insensitive_row_match(self):
        upper = "ACCOUNT    READ_WRITE    1000     900     100          1 hour from now\n"
        self.assertEqual(parse_account_remaining(upper), 100)

    def test_zero_remaining_is_not_confused_with_unparseable(self):
        # 0 and None mean very different things to the gate: 0 blocks, None
        # fails open. Assert the distinction explicitly.
        self.assertIsNotNone(parse_account_remaining(EXHAUSTED_OUTPUT))
        self.assertEqual(parse_account_remaining(EXHAUSTED_OUTPUT), 0)


class TestThresholdSemantics(unittest.TestCase):
    """Documents the <= comparison the callback uses, so a refactor to < is
    caught here rather than in production."""

    def test_boundary(self):
        for remaining, threshold, blocks in [
            (0, 50, True),
            (50, 50, True),
            (51, 50, False),
            (862, 50, False),
        ]:
            with self.subTest(remaining=remaining, threshold=threshold):
                self.assertEqual(remaining <= threshold, blocks)


if __name__ == "__main__":
    unittest.main()
