"""Per-vdev latency histograms, the gap #218 is about.

On 2026-10-06 tank's mirror-2 ran roughly 50x the I/O latency of its three
sibling vdevs, 58ms read against 1ms, and every existing signal was green:
pool ONLINE, smart_device_health 1 on both members, zero reallocated, pending,
uncorrectable and CRC, zero ata link resets. A vdev can be fifty times slower
than its peers with nothing at all to show for it.

These tests drive the real script against a stub `zpool` serving a fixture
captured from the live NAS on 2026-10-15
(`tests/fixtures/zpool-iostat-w-tank-20261015.tsv`, `zpool iostat -Hp -w -v
tank`), so the parser is exercised against the actual output shape rather than
one invented for the test.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "roles" / "truenas" / "files" / "zfs-smart-textfile.sh"
FIXTURE = REPO / "tests" / "fixtures" / "zpool-iostat-w-tank-20261015.tsv"

# The coarse boundaries the collector folds zpool's 37 native buckets into.
LE = ["0.0001", "0.0005", "0.001", "0.005", "0.01", "0.05", "0.1", "0.5", "1", "5", "+Inf"]
METRICS = [
    "zfs_vdev_total_wait_read_seconds",
    "zfs_vdev_total_wait_write_seconds",
    "zfs_vdev_disk_wait_read_seconds",
    "zfs_vdev_disk_wait_write_seconds",
]


def run_collector(tmp: str) -> str:
    tmp = Path(tmp)
    bin_dir, out_dir, dev_dir = tmp / "bin", tmp / "out", tmp / "dev"
    for d in (bin_dir, out_dir, dev_dir):
        d.mkdir(parents=True, exist_ok=True)

    # A zpool that answers the three invocations the script makes. Anything
    # else exits 0 silently, the same as the existing harness.
    (bin_dir / "zpool").write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            case "$1 $2" in
              "list -Hp")
                 case "$*" in
                   *fragmentation*) printf 'tank\\t100\\t50\\t50\\tONLINE\\t5\\t50\\n' ;;
                   *) printf 'tank\\n' ;;
                 esac ;;
              "iostat -Hp") cat {FIXTURE} ;;
              *) : ;;
            esac
            exit 0
            """
        )
    )
    (bin_dir / "zpool").chmod(0o755)
    # smartctl must exist but has nothing to report: no disks in the glob.
    (bin_dir / "smartctl").write_text("#!/bin/sh\nexit 2\n")
    (bin_dir / "smartctl").chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["TEXTFILE_DIR"] = str(out_dir)
    env["SMART_DEV_GLOB"] = f"{dev_dir}/sd?"
    proc = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, f"script failed: {proc.stderr}"
    written = out_dir / "zfs_smart.prom"
    assert written.exists(), f"nothing written; stderr: {proc.stderr}"
    return written.read_text()


@pytest.fixture(scope="module")
def out() -> str:
    with TemporaryDirectory() as tmp:
        return run_collector(tmp)


def buckets(out: str, metric: str, vdev: str) -> dict[str, int]:
    found = {}
    for line in out.splitlines():
        m = re.match(
            rf'^{metric}_bucket\{{pool="tank",vdev="{re.escape(vdev)}",le="([^"]+)"\}} (\d+)$',
            line,
        )
        if m:
            found[m.group(1)] = int(m.group(2))
    return found


def test_the_fixture_is_the_real_thing():
    """Guards against the fixture being quietly replaced by a toy."""
    text = FIXTURE.read_text()
    assert len(text.splitlines()) > 500
    for vdev in ("tank", "mirror-0", "mirror-1", "mirror-2", "mirror-3"):
        assert f"\n{vdev}\n" in text


@pytest.mark.parametrize("metric", METRICS)
@pytest.mark.parametrize("vdev", ["mirror-0", "mirror-1", "mirror-2", "mirror-3"])
def test_every_vdev_gets_a_complete_histogram(out, metric, vdev):
    b = buckets(out, metric, vdev)
    assert list(b.keys()) == LE, f"{metric}{{{vdev}}} buckets: {list(b.keys())}"


@pytest.mark.parametrize("metric", METRICS)
@pytest.mark.parametrize("vdev", ["mirror-0", "mirror-1", "mirror-2", "mirror-3"])
def test_buckets_are_cumulative(out, metric, vdev):
    """histogram_quantile() reads le= as cumulative. Emitting the raw
    per-bucket counts zpool gives would make every quantile wrong rather than
    obviously broken."""
    vals = [buckets(out, metric, vdev)[k] for k in LE]
    assert vals == sorted(vals), f"{metric}{{{vdev}}} not monotonic: {vals}"


@pytest.mark.parametrize("metric", METRICS)
def test_count_equals_the_inf_bucket(out, metric):
    for vdev in ("mirror-0", "mirror-1", "mirror-2", "mirror-3"):
        inf = buckets(out, metric, vdev)["+Inf"]
        line = f'{metric}_count{{pool="tank",vdev="{vdev}"}} {inf}'
        assert line in out, f"missing or mismatched: {line}"


def test_no_observations_are_dropped(out):
    """Every native bucket must land in exactly one coarse bin.

    Folding 37 buckets into 11 is where counts get silently lost, and a
    histogram that quietly drops its tail understates p99 precisely when the
    tail is the thing being investigated.
    """
    want = 0
    section = None
    for line in FIXTURE.read_text().splitlines():
        f = line.split("\t")
        if len(f) == 1:
            section = f[0].strip() or None
        elif section == "mirror-2" and len(f) >= 12 and f[0].isdigit():
            want += int(f[1])  # total_wait read column
    got = buckets(out, "zfs_vdev_total_wait_read_seconds", "mirror-2")["+Inf"]
    assert got == want, f"collector counted {got}, fixture holds {want}"


def test_leaf_vdevs_are_reported_too(out):
    """The top-level comparison says WHICH mirror; the members say which disk.
    #218 found both members of mirror-2 affected, which is what ruled out a
    single bad drive."""
    for uuid in ("6ac2c0dd-5a25-4136-b9c1-8daa40d66dc5",
                 "1683d319-2e4c-44d3-8550-1a74f7b2826f"):
        assert buckets(out, "zfs_vdev_total_wait_read_seconds", uuid), (
            f"no histogram for leaf {uuid}"
        )


def test_the_metrics_are_declared_as_histograms(out):
    for metric in METRICS:
        assert f"# TYPE {metric} histogram" in out


def test_the_existing_pool_metrics_still_work(out):
    """The latency emitter is spliced into the same output block, so a stray
    newline or an unflushed section would corrupt what was already there."""
    assert 'zfs_pool_health{pool="tank"} 0' in out
    assert "zfs_smart_textfile_last_run_timestamp_seconds " in out
