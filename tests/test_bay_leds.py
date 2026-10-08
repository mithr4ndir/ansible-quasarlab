"""The front-panel bay LEDs must point at the tray to pull, and never lie.

bay-leds.sh reads the zfs-smart-textfile output and per-disk block counters and
drives the UGREEN LED CLI. These tests run the real script against a fake
sysfs, a fake textfile, and a stub CLI that records every call, so what each
bay would show can be asserted without the hardware.

The failure that motivated it: on 2026-10-08 the bay 3 drive was FAULTED for
half an hour and the panel showed nothing; it had to be lit by hand.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "roles" / "truenas" / "files" / "bay-leds.sh"

RED = "-color 255 0 0 -on -brightness 255"
AMBER = "-color 255 120 0 -blink 500 500 -brightness 255"
IDLE = "-color 255 255 255 -on -brightness 40"
ACTIVE = "-color 255 255 255 -blink 80 80 -brightness 200"
OFF = "-off"
NODATA = "-color 160 0 255 -breath 2000 2000 -brightness 120"


class Panel:
    def __init__(self, tmp: str, disks: dict[int, str]):
        self.root = Path(tmp)
        self.cls = self.root / "sys" / "class" / "block"
        self.cls.mkdir(parents=True)
        self.out = self.root / "textfiles"
        self.out.mkdir()
        self.log = self.root / "cli.log"
        self.log.touch()
        for bay, name in disks.items():
            self.add_disk(bay, name)
        # The stub records each call. It also bumps the I/O counter of a disk
        # named in BUMP_DEV, which makes "activity between two polls"
        # deterministic instead of depending on a race with a sleeping test.
        self.cli = self.root / "ugreen_leds_cli"
        self.cli.write_text(
            "#!/bin/sh\n"
            f'LOG="{self.log}"; echo "$*" >> "$LOG"\n'
            'if [ -n "${BUMP_DEV:-}" ] && [ "$1" = "${BUMP_ON:-$1}" ] \\\n'
            '   && [ "${BUMP_CALLS:-999}" -ge "$(grep -c "^$1 " "$LOG")" ]; then\n'
            f'  f="{self.cls}/$BUMP_DEV/stat"; set -- $(cat "$f")\n'
            '  echo "$(($1 + 1)) 0 0 0 $5 0 0 0" > "$f"\n'
            "fi\n"
        )
        self.cli.chmod(0o755)
        self.env = dict(os.environ)
        self.env.update(
            LED_CLI=str(self.cli),
            SYS_CLASS_BLOCK=str(self.cls),
            TEXTFILE_DIR=str(self.out),
            HOLD_FILE=str(self.root / "hold"),
            POLL_SECONDS="0",
            ITERATIONS="1",
        )

    def add_disk(self, bay: int, name: str) -> None:
        dev = self.root / "sys" / "devices" / f"ata{bay}" / f"host{bay - 1}" / "block" / name
        (dev / f"{name}1").mkdir(parents=True)
        (dev / "device").mkdir()
        (dev / "stat").write_text("100 0 0 0 50 0 0 0\n")
        (self.cls / name).symlink_to(dev)
        # A partition entry, which must not be mistaken for a disk.
        (self.cls / f"{name}1").symlink_to(dev / f"{name}1")

    def metrics(self, lines: list[str], age: int = 0) -> None:
        ts = int(time.time()) - age
        body = "\n".join(lines + [f"zfs_smart_textfile_last_run_timestamp_seconds {ts}"])
        (self.out / "zfs_smart.prom").write_text(body + "\n")

    def run(self, *args: str, **env: str) -> dict[int, list[str]]:
        e = dict(self.env, **env)
        proc = subprocess.run(
            ["bash", str(SCRIPT), *args], env=e, capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        calls: dict[int, list[str]] = {}
        for line in self.log.read_text().splitlines():
            led, _, rest = line.partition(" ")
            calls.setdefault(int(led.removeprefix("disk")), []).append(rest)
        return calls


def vdev(bay: str, value: int, metric: str = "zfs_vdev_state") -> str:
    return (f'{metric}{{pool="tank",vdev="mirror-3",guid="1",name="u",device="sdd",'
            f'bay="{bay}",serial="S{bay}"}} {value}')


def test_faulted_disk_turns_its_bay_red_and_the_rest_stay_truthful():
    with TemporaryDirectory() as tmp:
        p = Panel(tmp, {3: "sdd", 5: "sdb"})
        p.metrics([vdev("3", 2), vdev("5", 0)])
        calls = p.run()
    assert calls[3] == [RED], calls
    assert calls[5] == [IDLE], calls
    # Bay 7 has no disk and no vdev: off, not a healthy white.
    assert calls[7] == [OFF], calls


def test_a_detached_disk_is_still_red_not_an_empty_bay():
    """After a drop-off the device node is gone but the exporter keeps the bay
    label from its cache. The tray must stay red, not go dark."""
    with TemporaryDirectory() as tmp:
        p = Panel(tmp, {5: "sdb"})
        p.metrics([vdev("3", 5)])
        calls = p.run()
    assert calls[3] == [RED], calls


def test_zfs_errors_and_unreadable_smart_are_amber():
    with TemporaryDirectory() as tmp:
        p = Panel(tmp, {2: "sdc", 4: "sde"})
        p.metrics([
            vdev("2", 0), vdev("2", 256, "zfs_vdev_write_errors"),
            'smart_scrape_ok{device="sde",bay="4",serial="X"} 0',
        ])
        calls = p.run()
    assert calls[2] == [AMBER], calls
    assert calls[4] == [AMBER], calls


def test_red_beats_amber_whichever_line_comes_first():
    for order in (0, 1):
        lines = [vdev("3", 9, "zfs_vdev_read_errors"), vdev("3", 2)]
        with TemporaryDirectory() as tmp:
            p = Panel(tmp, {3: "sdd"})
            p.metrics(lines if order else lines[::-1])
            calls = p.run()
        assert calls[3] == [RED], (order, calls)


def test_activity_blinks_and_idle_returns_to_dim():
    with TemporaryDirectory() as tmp:
        p = Panel(tmp, {6: "sdg"})
        p.metrics([vdev("6", 0)])
        calls = p.run(ITERATIONS="3", BUMP_DEV="sdg")
    # Poll 1 paints idle (no previous counter); that write bumps the counter,
    # so poll 2 sees I/O and blinks; the blink write bumps it again, so poll 3
    # still sees I/O and must NOT re-write an unchanged state.
    assert calls[6] == [IDLE, ACTIVE], calls


def test_activity_holds_briefly_then_returns_to_idle():
    """Only the first two CLI calls produce I/O. The bay must keep blinking for
    the hold window after the last I/O, then go back to dim, so ZFS's flush
    bursts do not make every bay flap on each poll."""
    with TemporaryDirectory() as tmp:
        p = Panel(tmp, {6: "sdg"})
        p.metrics([vdev("6", 0)])
        # Only bay 6's own first write produces I/O. Poll 1 paints idle
        # (bump), poll 2 sees it: active (no more bumps). Poll 3 sees none
        # but is inside the 2-poll hold. Poll 4 is past it: idle.
        calls = p.run(ITERATIONS="6", BUMP_DEV="sdg", BUMP_ON="disk6", BUMP_CALLS="1")
    assert calls[6] == [IDLE, ACTIVE, IDLE], calls

    # Same, stopped at poll 3: one poll after the I/O it must still blink,
    # which is what distinguishes a hold from no hold at all.
    with TemporaryDirectory() as tmp:
        p = Panel(tmp, {6: "sdg"})
        p.metrics([vdev("6", 0)])
        calls = p.run(ITERATIONS="3", BUMP_DEV="sdg", BUMP_ON="disk6", BUMP_CALLS="1")
    assert calls[6] == [IDLE, ACTIVE], calls


def test_an_unchanged_panel_is_written_once_not_every_poll():
    """The controller shares the I2C bus with board sensors."""
    with TemporaryDirectory() as tmp:
        p = Panel(tmp, {1: "sda", 3: "sdd"})
        p.metrics([vdev("1", 0), vdev("3", 2)])
        calls = p.run(ITERATIONS="5")
    assert all(len(v) == 1 for v in calls.values()), calls


def test_stale_or_missing_fault_data_shows_no_data_rather_than_healthy():
    for setup in ("stale", "missing"):
        with TemporaryDirectory() as tmp:
            p = Panel(tmp, {3: "sdd"})
            if setup == "stale":
                p.metrics([vdev("3", 0)], age=3600)
            calls = p.run()
        assert set(calls) == set(range(1, 9)), (setup, calls)
        assert all(v == [NODATA] for v in calls.values()), (setup, calls)


def test_hold_file_pauses_all_writes_for_manual_identification():
    with TemporaryDirectory() as tmp:
        p = Panel(tmp, {3: "sdd"})
        p.metrics([vdev("3", 2)])
        (p.root / "hold").touch()
        calls = p.run(ITERATIONS="3")
    assert calls == {}, calls


def test_stopped_paints_every_bay_as_not_running():
    with TemporaryDirectory() as tmp:
        p = Panel(tmp, {3: "sdd"})
        calls = p.run("--stopped")
    assert calls == {b: [NODATA] for b in range(1, 9)}, calls


def test_heartbeat_reports_fresh_data_for_the_staleness_alert():
    with TemporaryDirectory() as tmp:
        p = Panel(tmp, {3: "sdd"})
        p.metrics([vdev("3", 0)])
        p.run()
        hb = (p.out / "bay_leds.prom").read_text().splitlines()
    assert "bay_leds_fault_data_fresh 1" in hb, hb
    ts = [l for l in hb if l.startswith("bay_leds_last_loop_timestamp_seconds ")]
    assert ts and abs(int(ts[0].split()[1]) - time.time()) < 60, hb


def test_service_unit_repaints_on_any_stop_and_loads_i2c():
    unit = (REPO / "roles" / "truenas" / "templates" / "bay-leds.service.j2").read_text()
    assert "ExecStopPost={{ truenas_exporter_dir }}/bay-leds.sh --stopped" in unit
    assert "ExecStartPre=/sbin/modprobe i2c-dev" in unit
    assert "Restart=always" in unit
