"""The backup cadence, and the three things that silently couple to it.

Run from the repo root with the pinned runner:
    uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" \
        pytest tests/test_pbs_backup_schedule.py -rs

On 2026-10-15 the PBS backup job moved from `hourly` to once a day. Each of
these tests exists because something that looks unrelated to a schedule string
breaks when that string changes, and all three were measured rather than
imagined.

1. A cluster backup job is evaluated by pvescheduler on EVERY node, and PVE
   parses a bare clock time in that NODE's local time. The two nodes disagree:

       schedule     pve (America/Los_Angeles)   pve2 (America/New_York)
       03:00        10:00 UTC                   07:00 UTC
       10:00 UTC    10:00 UTC                   10:00 UTC

   So a bare "03:00" does not mean "3am", it means two runs three hours apart,
   with pve2 starting at midnight Pacific. A named zone does not help: PVE's
   parser rejects "03:00 America/Los_Angeles" outright. UTC is the only suffix
   it accepts. `hourly` and friends are instants everywhere and need no pin.

2. PBS `keep-*` options count BUCKETS THAT CONTAIN SNAPSHOTS, not clock time.
   `keep-hourly 24` means "the last 24 hours" only while backups are hourly.
   One backup a day puts each snapshot in its own hour bucket, so the same 24
   would mean the last 24 DAYS. Changing cadence alone would have roughly
   tripled the daily tail on a datastore already at 56% of 590G, with nothing
   in the diff to show it.

3. Prune has to run after the backup window, not inside it. That comparison is
   only possible because (1) forces the PVE side to be expressed in UTC and both
   PBS guests run UTC, so the two literals can be compared at all.
"""

from __future__ import annotations

import re

import pytest
import yaml

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PVE_DEFAULTS = REPO / "roles" / "pve" / "pbs_storage" / "defaults" / "main.yml"
PBS_DEFAULTS = REPO / "roles" / "pbs" / "defaults" / "main.yml"

# systemd shorthands are complete expressions that denote the same instant on
# every host, so they carry no timezone ambiguity.
SHORTHANDS = frozenset({"minutely", "hourly", "daily", "weekly", "monthly", "yearly"})

DAILY_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s+UTC)?$")


def _defaults(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def needs_utc_pin(spec: str) -> bool:
    """True when this spec would resolve to a different instant per node.

    Anything carrying a clock time is evaluated in the evaluating host's local
    timezone unless it says UTC. The shorthands do not.
    """
    spec = spec.strip()
    if spec.lower() in SHORTHANDS:
        return False
    return not spec.endswith("UTC")


def daily_utc_minutes(spec: str) -> int | None:
    """Minutes past midnight UTC for a plain once-a-day spec, else None."""
    match = DAILY_CLOCK.match(spec.strip())
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


class TestTheGuardItself:
    """The helper has to fail the real defect, or the tests below are theatre."""

    @pytest.mark.parametrize("spec", ["03:00", "03:00:00", "Tue *-*-* 03:00:00", "*-*-* 04:00:00"])
    def test_a_bare_clock_time_is_rejected(self, spec):
        assert needs_utc_pin(spec)

    @pytest.mark.parametrize("spec", ["hourly", "10:00 UTC", "Tue *-*-* 03:00:00 UTC"])
    def test_an_unambiguous_spec_is_accepted(self, spec):
        assert not needs_utc_pin(spec)

    def test_only_a_plain_daily_time_parses_as_one(self):
        assert daily_utc_minutes("10:00 UTC") == 600
        assert daily_utc_minutes("12:00") == 720
        assert daily_utc_minutes("hourly") is None
        assert daily_utc_minutes("Tue *-*-* 03:00:00") is None
        assert daily_utc_minutes("25:00") is None


def test_the_cluster_backup_job_cannot_resolve_per_node():
    """A wall-clock PVE cluster schedule must pin UTC. See (1) in the docstring."""
    spec = _defaults(PVE_DEFAULTS)["pbs_backup_schedule"]
    assert not needs_utc_pin(spec), (
        f"pbs_backup_schedule {spec!r} carries a clock time with no UTC suffix. "
        "pvescheduler evaluates it on every node in that node's local time, and "
        "pve is America/Los_Angeles while pve2 is America/New_York, so this is "
        "two runs three hours apart rather than one daily backup."
    )


def test_keep_hourly_matches_the_backup_cadence():
    """keep-hourly counts hour buckets holding snapshots. See (2)."""
    schedule = _defaults(PVE_DEFAULTS)["pbs_backup_schedule"]
    keep_hourly = int(_defaults(PBS_DEFAULTS)["pbs_prune_keep_hourly"])

    backups_are_hourly = schedule.strip().lower() in {"hourly", "minutely"}
    if backups_are_hourly:
        assert keep_hourly > 1
        return

    assert keep_hourly <= 1, (
        f"backups run on schedule {schedule!r}, at most once a day, but "
        f"keep-hourly is {keep_hourly}. PBS keeps the newest snapshot in each of "
        f"the last {keep_hourly} hour buckets that contain one, so with daily "
        f"snapshots that retains {keep_hourly} DAYS, not {keep_hourly} hours. "
        "Use 1, which retains nothing keep-daily does not already retain."
    )


def test_prune_runs_after_the_backup_window_not_inside_it():
    """Prune must follow the backup. See (3)."""
    backup = daily_utc_minutes(_defaults(PVE_DEFAULTS)["pbs_backup_schedule"])
    prune = daily_utc_minutes(_defaults(PBS_DEFAULTS)["pbs_prune_schedule"])
    if backup is None or prune is None:
        pytest.skip("one side is not a plain once-a-day time; nothing to compare")

    # The slowest node took 17 minutes on hourly deltas. A daily run carries 24h
    # of dirty blocks, so hold an hour clear before pruning the group it writes.
    assert prune - backup >= 60, (
        f"prune starts {prune - backup} minutes after the backup window opens "
        f"(backup {backup // 60:02d}:{backup % 60:02d} UTC, prune "
        f"{prune // 60:02d}:{prune % 60:02d} UTC). Both PBS guests run UTC and "
        "the PVE job is UTC-pinned, so these are directly comparable. Pruning a "
        "group while vzdump is still writing it is not worth the risk."
    )
