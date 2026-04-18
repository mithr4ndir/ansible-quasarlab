#!/usr/bin/env python3
"""Convert `op service-account ratelimit` table output to Prometheus textfile.

Reads the CLI table from stdin, writes Prometheus textfile metrics to stdout.
Exits 2 with a one-line error on stderr if the input is malformed.

The 1Password CLI's ratelimit command does not support JSON output (as of
2026-04). When it eventually does, replace this parser with a json loader.
"""

from __future__ import annotations

import re
import sys
from typing import Optional


EXPECTED_HEADER_COLS = ["TYPE", "ACTION", "LIMIT", "USED", "REMAINING", "RESET"]


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def parse_reset(cell: str) -> Optional[int]:
    """Convert reset strings like '5 hours from now' or '23 hours and 59 minutes from now' to seconds. Returns None for 'N/A'."""
    cell = cell.strip()
    if not cell or cell.upper() == "N/A":
        return None
    m = re.match(
        r"^(?:(\d+)\s+hours?)?(?:\s*and\s*)?(?:(\d+)\s+minutes?)?\s+from now$",
        cell,
        re.IGNORECASE,
    )
    if not m or (not m.group(1) and not m.group(2)):
        fail(f"unrecognized reset value: {cell!r}")
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    return hours * 3600 + minutes * 60


def parse_int(cell: str, col: str) -> int:
    cell = cell.strip()
    try:
        return int(cell)
    except ValueError:
        fail(f"expected integer for {col}, got {cell!r}")
        return 0  # unreachable, satisfies type checker


def emit(rows: list[dict]) -> None:
    out: list[str] = []
    # used
    out.append("# HELP onepassword_ratelimit_used Requests used in the current window.")
    out.append("# TYPE onepassword_ratelimit_used gauge")
    for r in rows:
        out.append(
            f'onepassword_ratelimit_used{{type="{r["type"]}",action="{r["action"]}"}} {r["used"]}'
        )
    # limit
    out.append("# HELP onepassword_ratelimit_limit Request limit for this window.")
    out.append("# TYPE onepassword_ratelimit_limit gauge")
    for r in rows:
        out.append(
            f'onepassword_ratelimit_limit{{type="{r["type"]}",action="{r["action"]}"}} {r["limit"]}'
        )
    # remaining
    out.append("# HELP onepassword_ratelimit_remaining Requests remaining before the window resets.")
    out.append("# TYPE onepassword_ratelimit_remaining gauge")
    for r in rows:
        out.append(
            f'onepassword_ratelimit_remaining{{type="{r["type"]}",action="{r["action"]}"}} {r["remaining"]}'
        )
    # reset_seconds (omit rows where reset is None)
    reset_rows = [r for r in rows if r["reset_seconds"] is not None]
    if reset_rows:
        out.append("# HELP onepassword_ratelimit_reset_seconds Seconds until the limit resets.")
        out.append("# TYPE onepassword_ratelimit_reset_seconds gauge")
        for r in reset_rows:
            out.append(
                f'onepassword_ratelimit_reset_seconds{{type="{r["type"]}",action="{r["action"]}"}} {r["reset_seconds"]}'
            )
    sys.stdout.write("\n".join(out) + "\n")


def main() -> None:
    raw = sys.stdin.read()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        fail("empty input")

    header = re.split(r"\s{2,}", lines[0].strip())
    if header != EXPECTED_HEADER_COLS:
        fail(f"unexpected header: {header!r}")

    rows: list[dict] = []
    for line in lines[1:]:
        cells = re.split(r"\s{2,}", line.strip())
        if len(cells) != len(EXPECTED_HEADER_COLS):
            fail(f"wrong column count in row: {line!r}")
        t, action, lim, used, rem, reset = cells
        rows.append(
            {
                "type": t,
                "action": action,
                "limit": parse_int(lim, "LIMIT"),
                "used": parse_int(used, "USED"),
                "remaining": parse_int(rem, "REMAINING"),
                "reset_seconds": parse_reset(reset),
            }
        )
    if not rows:
        fail("no data rows")
    emit(rows)


if __name__ == "__main__":
    main()
