"""Behaviour tests for roles/pbs/files/pbs-backup-collector.sh.

These build a real datastore tree on disk and run the real script against it.
Nothing is mocked, because the thing worth testing is precisely how the script
reads the filesystem.

The assertion that matters is that a snapshot DIRECTORY is not evidence of a
backup. PBS writes index.json.blob last, so an aborted backup leaves a
directory with no manifest. A collector that counted directories would report
a fresh backup for a guest that has not been backed up successfully in weeks,
and the staleness alert built on it would be decorative.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "files" / "pbs-backup-collector.sh"


def _snapshot(guest_dir: Path, stamp: str, *, manifest: bool, disks: int = 1) -> Path:
    snap = guest_dir / stamp
    snap.mkdir(parents=True)
    if manifest:
        (snap / "index.json.blob").write_bytes(b"\x01binary manifest")
    (snap / "client.log.blob").write_bytes(b"log")
    for i in range(disks):
        (snap / f"drive-scsi{i}.img.fidx").write_bytes(b"idx")
    return snap


def _run(tmp_path: Path) -> dict:
    """Run the collector and parse its output into {series: value}."""
    prom = tmp_path / "out.prom"
    res = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin",
            "PBS_DATASTORE_ROOT": str(tmp_path / "datastore"),
            "PBS_COLLECTOR_PROM_FILE": str(prom),
        },
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"collector failed: {res.stderr}"
    out = {}
    for line in prom.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        series, _, value = line.rpartition(" ")
        out[series] = float(value)
    return out


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "datastore" / "lab" / "vm"
    d.mkdir(parents=True)
    return d


def test_complete_snapshot_is_reported(store, tmp_path):
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=True)
    out = _run(tmp_path)
    key = 'pbs_snapshot_last_complete_timestamp_seconds{datastore="lab",guest_type="vm",guest="100"}'
    assert key in out
    # 2026-10-15T01:00:00Z
    assert out[key] == 1792026000
    assert out['pbs_snapshot_disk_index_count{datastore="lab",guest_type="vm",guest="100"}'] == 1


def test_snapshot_without_manifest_is_not_a_backup(store, tmp_path):
    """The regression this collector exists to prevent.

    An aborted backup leaves a directory behind. Counting directories would
    report it as a successful, fresh backup.
    """
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=False)
    out = _run(tmp_path)
    assert not [k for k in out if "guest=\"100\"" in k and "last_complete" in k]
    assert out["pbs_backup_collector_guests_without_complete"] == 1
    assert out["pbs_backup_collector_guests_total"] == 1


def test_newer_incomplete_does_not_mask_older_complete(store, tmp_path):
    """A failing hourly job must not keep the metric looking fresh."""
    _snapshot(store / "100", "2026-10-10T01:00:00Z", manifest=True)
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=False)
    out = _run(tmp_path)
    key = 'pbs_snapshot_last_complete_timestamp_seconds{datastore="lab",guest_type="vm",guest="100"}'
    # The OLD complete one, not the new broken one.
    assert out[key] == 1791594000
    assert out["pbs_backup_collector_guests_without_complete"] == 0


def test_manifest_with_no_disks_reports_zero(store, tmp_path):
    """The PBS form of the backup=0 trap: a backup that restores nothing.

    It completed, so it is not stale, and only the disk count reveals it.
    """
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=True, disks=0)
    out = _run(tmp_path)
    assert out['pbs_snapshot_disk_index_count{datastore="lab",guest_type="vm",guest="100"}'] == 0


def test_guest_with_only_an_owner_file(store, tmp_path):
    """Exactly the state pbs1 itself is in: registered, never backed up.

    It is VISIBLE as dir_count 0 and deliberately not alertable. pbs1 is
    excluded from the hourly job, and a backup that fails before PBS creates
    a snapshot directory leaves nothing else behind, so an alert on
    "registered but never completed" would be permanently red on this lab.
    A permanently firing alert is worse than no alert.
    """
    g = store / "103"
    g.mkdir()
    (g / "owner").write_text("root@pam\n")
    out = _run(tmp_path)
    assert out["pbs_backup_collector_guests_without_complete"] == 1
    assert out['pbs_guest_snapshot_dir_count{datastore="lab",guest_type="vm",guest="103"}'] == 0
    assert not [k for k in out if 'guest="103"' in k and "last_complete" in k]


def test_a_guest_whose_every_attempt_failed_is_alertable(store, tmp_path):
    """The case the aggregate gauge cannot separate from pbs1's.

    Attempts happened, PBS created the directories, and not one carries a
    manifest. dir_count > 0 with no last_complete series is unambiguous, and
    it is what the PbsGuestBackupsAllFailing rule keys on.
    """
    _snapshot(store / "121", "2026-10-14T01:00:00Z", manifest=False)
    _snapshot(store / "121", "2026-10-15T01:00:00Z", manifest=False)
    out = _run(tmp_path)
    assert out['pbs_guest_snapshot_dir_count{datastore="lab",guest_type="vm",guest="121"}'] == 2
    assert not [k for k in out if 'guest="121"' in k and "last_complete" in k]


def test_dir_count_covers_the_whole_retention_window(store, tmp_path):
    """The walk stops caring about timestamps at the first manifest.

    It must not stop WALKING there. The snapshots are listed newest first, so
    an early `break` would count one directory for a guest holding a full
    retention window of good backups and report the guest as nearly empty.
    The fixture therefore puts the manifest on the NEWEST snapshot, which is
    the normal case and the one an early exit gets wrong.
    """
    for day in ("13", "14", "15"):
        _snapshot(store / "100", f"2026-10-{day}T01:00:00Z", manifest=True)
    out = _run(tmp_path)
    assert out['pbs_guest_snapshot_dir_count{datastore="lab",guest_type="vm",guest="100"}'] == 3
    key = 'pbs_snapshot_last_complete_timestamp_seconds{datastore="lab",guest_type="vm",guest="100"}'
    # Still the NEWEST, not the last one walked.
    assert out[key] == 1792026000


def test_dir_count_includes_failures_newer_than_the_newest_complete(store, tmp_path):
    _snapshot(store / "100", "2026-10-13T01:00:00Z", manifest=True)
    _snapshot(store / "100", "2026-10-14T01:00:00Z", manifest=False)
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=False)
    out = _run(tmp_path)
    assert out['pbs_guest_snapshot_dir_count{datastore="lab",guest_type="vm",guest="100"}'] == 3
    key = 'pbs_snapshot_last_complete_timestamp_seconds{datastore="lab",guest_type="vm",guest="100"}'
    assert out[key] == 1791853200


def test_label_injection_is_rejected(store, tmp_path):
    """Guest names become label values, so a crafted directory must be skipped."""
    bad = store / 'evil",fake="1'
    bad.mkdir()
    _snapshot(bad, "2026-10-15T01:00:00Z", manifest=True)
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=True)
    out = _run(tmp_path)
    assert not [k for k in out if "fake=" in k], "injected label reached the output"
    assert any('guest="100"' in k for k in out)


def test_non_backup_directories_are_ignored(store, tmp_path):
    """lost+found sits next to the backup type directories."""
    (store.parent.parent / "lab" / "lost+found").mkdir(exist_ok=True)
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=True)
    out = _run(tmp_path)
    assert out["pbs_backup_collector_guests_total"] == 1


def test_output_is_valid_exposition_format(store, tmp_path):
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=True)
    prom = tmp_path / "out.prom"
    subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin",
            "PBS_DATASTORE_ROOT": str(tmp_path / "datastore"),
            "PBS_COLLECTOR_PROM_FILE": str(prom),
        },
        check=True,
        capture_output=True,
    )
    text = prom.read_text()
    assert text.endswith("\n")
    for line in text.splitlines():
        if line.startswith("#"):
            assert line.startswith(("# HELP ", "# TYPE "))
            continue
        series, _, value = line.rpartition(" ")
        assert series and value
        float(value)
    # Every metric carries HELP and TYPE, which is what makes it legible in
    # Grafana and what promtool checks.
    for metric in (
        "pbs_snapshot_last_complete_timestamp_seconds",
        "pbs_snapshot_disk_index_count",
        "pbs_backup_collector_guests_total",
        "pbs_backup_collector_guests_without_complete",
        "pbs_backup_collector_timestamp_seconds",
        "pbs_guest_snapshot_dir_count",
    ):
        assert f"# HELP {metric} " in text
        assert f"# TYPE {metric} " in text


def test_heartbeat_is_present_even_with_an_empty_datastore(tmp_path):
    (tmp_path / "datastore").mkdir()
    out = _run(tmp_path)
    assert out["pbs_backup_collector_guests_total"] == 0
    assert out["pbs_backup_collector_timestamp_seconds"] > 0


def test_partial_write_is_not_exposed(store, tmp_path):
    """node_exporter reads the directory continuously, so the swap is atomic."""
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=True)
    prom = tmp_path / "out.prom"
    prom.write_text("pbs_stale_marker 1\n")
    _run(tmp_path)
    assert "pbs_stale_marker" not in prom.read_text()
    # And no temp files left lying next to it for node_exporter to trip over.
    assert not list(tmp_path.glob("out.prom.*"))


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed")
def test_promtool_accepts_the_output(store, tmp_path):
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=True)
    prom = tmp_path / "out.prom"
    subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin",
            "PBS_DATASTORE_ROOT": str(tmp_path / "datastore"),
            "PBS_COLLECTOR_PROM_FILE": str(prom),
        },
        check=True,
        capture_output=True,
    )
    res = subprocess.run(
        ["promtool", "check", "metrics"],
        stdin=prom.open("rb"),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr


# ---------------------------------------------------------------------------
# Refusing to publish an incomplete file.
#
# errexit is off on purpose, so every write into the temp file is checked by
# hand. Without that, a filesystem that fills up mid-run leaves a TRUNCATED
# file, renames it over the last good metrics and exits 0. The series that
# failed to write then vanish, and a vanished snapshot timestamp reads as
# "this guest has never been backed up" while a vanished heartbeat reads as a
# collector that is still running fine. Stale metrics are recoverable;
# confidently wrong ones are not.
# ---------------------------------------------------------------------------

REAL_DATE = shutil.which("date") or "/bin/date"


def _shim(tmp_path: Path, name: str, body: str) -> str:
    """Put an executable shim ahead of the real tool on PATH."""
    d = tmp_path / "shim"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(body)
    p.chmod(0o755)
    return f"{d}:/usr/bin:/bin"


def _run_raw(tmp_path: Path, path: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": path or "/usr/bin:/bin",
            "PBS_DATASTORE_ROOT": str(tmp_path / "datastore"),
            "PBS_COLLECTOR_PROM_FILE": str(tmp_path / "out.prom"),
        },
        capture_output=True,
        text=True,
    )


def test_a_missing_heartbeat_keeps_the_previous_file(store, tmp_path):
    """The heartbeat is written last, so it is the proof the file is whole.

    Driven by making `date +%s` fail, which leaves every guest series correct
    and only the final heartbeat valueless. That is the narrowest possible
    version of a truncated file: if the collector published it, node_exporter
    would reject the whole file for malformed exposition and every PBS series
    would disappear at once.
    """
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=True)
    prom = tmp_path / "out.prom"
    prom.write_text("pbs_backup_collector_timestamp_seconds 1792000000\n")
    before = prom.read_bytes()

    path = _shim(
        tmp_path,
        "date",
        f'#!/bin/sh\nif [ "$1" = "+%s" ]; then exit 1; fi\nexec {REAL_DATE} "$@"\n',
    )
    res = _run_raw(tmp_path, path)

    assert res.returncode != 0, "published a file with no valid heartbeat"
    assert prom.read_bytes() == before, "overwrote good metrics with an incomplete file"
    assert not list(tmp_path.glob("out.prom.*")), "left a temp file behind"


def test_a_chmod_failure_keeps_the_previous_file(store, tmp_path):
    """Published metrics node_exporter cannot read are worse than stale ones."""
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=True)
    prom = tmp_path / "out.prom"
    prom.write_text("pbs_backup_collector_timestamp_seconds 1792000000\n")
    before = prom.read_bytes()

    path = _shim(tmp_path, "chmod", "#!/bin/sh\nexit 1\n")
    res = _run_raw(tmp_path, path)

    assert res.returncode != 0
    assert prom.read_bytes() == before
    assert not list(tmp_path.glob("out.prom.*"))


def test_the_happy_path_still_publishes(store, tmp_path):
    """Guard against a guard that rejects everything.

    Both tests above would pass against a script whose publish step always
    failed, so the same fixture has to succeed when nothing is broken.
    """
    _snapshot(store / "100", "2026-10-15T01:00:00Z", manifest=True)
    prom = tmp_path / "out.prom"
    prom.write_text("pbs_backup_collector_timestamp_seconds 1\n")
    res = _run_raw(tmp_path)
    assert res.returncode == 0, res.stderr
    assert "pbs_backup_collector_timestamp_seconds 1\n" != prom.read_text()
    assert 'guest="100"' in prom.read_text()


def test_every_write_into_the_temp_file_is_checked():
    """Structural, so a future unguarded write is caught at review time.

    The failure mode is invisible at runtime: an unchecked `>>` on a full
    filesystem drops a line and reports success.
    """
    unguarded = []
    for n, line in enumerate(SCRIPT.read_text().splitlines(), 1):
        stripped = line.strip()
        if '>"$tmp"' not in stripped and '>>"$tmp"' not in stripped:
            continue
        if "note_write_failure" in stripped:
            continue
        unguarded.append(f"{n}: {stripped}")
    assert not unguarded, (
        "these writes into the temp file are unchecked, so a short write would "
        "publish a truncated file: " + "; ".join(unguarded)
    )
