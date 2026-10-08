"""Per-disk ZFS state, labelled with the bay a person has to pull.

On 2026-10-08 the bay 3 drive (IB24AK0004S00064) dropped off the SATA bus and
ZFS marked it FAULTED, yet `zpool list` kept reporting the pool ONLINE, so
`zfs_pool_health` never moved and ZfsPoolNotOnline never fired. The only alerts
that did fire named the disk as `sdd`, a letter that changes between boots.
These tests drive the real script against a stub `zpool` and a fake sysfs.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from test_zfs_smart_textfile import HEALTHY, Harness, samples

TANK_GUID_BAY3 = "16921602523873656744"
TANK_GUID_BAY5 = "6314399114229375679"
UUID_BAY3 = "701d7b32-63bb-4ae2-8501-73261bcff843"
UUID_BAY5 = "339fd304-fce6-45e9-8e63-bb0c164d8bfd"


def fake_disk(root: Path, name: str, port: int, serial: str) -> None:
    """Build the sysfs shape the script reads for one SATA disk with one partition."""
    sdev = root / "sys" / "devices" / "pci0" / f"ata{port}" / f"host{port - 1}" / f"{port - 1}:0:0:0"
    block = sdev / "block" / name
    (block / f"{name}1").mkdir(parents=True)
    # VPD page 80: 4-byte header, then the space-padded serial. The length byte
    # here is 0x41 ('A'), a printable character, to prove the header is skipped
    # by position rather than by filtering out unprintable bytes.
    (sdev / "vpd_pg80").write_bytes(b"\x00\x80\x00\x41" + serial.ljust(20).encode())
    (block / "device").symlink_to(sdev)
    cls = root / "sys" / "class" / "block"
    cls.mkdir(parents=True, exist_ok=True)
    (cls / name).symlink_to(block)
    (cls / f"{name}1").symlink_to(block / f"{name}1")


def partuuid_link(root: Path, uuid: str, part: str) -> Path:
    by = root / "dev" / "disk" / "by-partuuid"
    by.mkdir(parents=True, exist_ok=True)
    (root / "dev" / part).touch()
    link = by / uuid
    link.symlink_to(root / "dev" / part)
    return link


def leaf(guid: str, uuid: str, path: Path | str, state: str, rd="0", wr="0", ck="0") -> dict:
    return {
        "name": uuid, "vdev_type": "disk", "guid": guid, "path": str(path),
        "class": "normal", "state": state,
        "read_errors": rd, "write_errors": wr, "checksum_errors": ck,
    }


def status_json(mirror3: list[dict], spares: dict | None = None) -> dict:
    """Shape of `zpool status -j -p`, trimmed from the real TrueNAS output.

    Note mirror-3 and the pool both say ONLINE even with a FAULTED child:
    that is what the NAS actually reported on 2026-10-08.
    """
    pool = {
        "name": "tank", "state": "ONLINE",
        "vdevs": {"tank": {
            "name": "tank", "vdev_type": "root", "state": "ONLINE",
            "vdevs": {"mirror-3": {
                "name": "mirror-3", "vdev_type": "mirror", "state": "ONLINE",
                "vdevs": {l["name"]: l for l in mirror3},
            }},
        }},
    }
    if spares:
        pool["spares"] = spares
    return {"output_version": {"command": "zpool status"}, "pools": {"tank": pool}}


class VdevHarness(Harness):
    def __init__(self, tmp: str, doc: dict | None, zpool_rc: int = 0):
        super().__init__(tmp, HEALTHY, 0)
        self.root = Path(tmp)
        self.env["SYS_CLASS_BLOCK"] = str(self.root / "sys" / "class" / "block")
        self.set_status(doc, zpool_rc)

    def set_status(self, doc: dict | None, zpool_rc: int = 0) -> None:
        payload = self.root / "status.json"
        payload.write_text(json.dumps(doc) if doc is not None else "")
        stub = self.root / "bin" / "zpool"
        stub.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = status ]; then cat "{payload}"; exit {zpool_rc}; fi\n'
            "exit 0\n"
        )
        stub.chmod(0o755)


def vdev(out: str, metric: str, guid: str) -> list[str]:
    return [l for l in samples(out, metric) if f'guid="{guid}"' in l]


def test_faulted_member_of_an_online_mirror_is_reported_with_its_bay():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_disk(root, "sdd", 3, "IB24AK0004S00064")
        fake_disk(root, "sdb", 5, "IB24AK0004S00004")
        doc = status_json([
            leaf(TANK_GUID_BAY3, UUID_BAY3, partuuid_link(root, UUID_BAY3, "sdd1"), "FAULTED", rd="3", wr="256"),
            leaf(TANK_GUID_BAY5, UUID_BAY5, partuuid_link(root, UUID_BAY5, "sdb1"), "ONLINE"),
        ])
        out = VdevHarness(tmp, doc).run()

    bad = vdev(out, "zfs_vdev_state", TANK_GUID_BAY3)
    assert bad == [
        f'zfs_vdev_state{{pool="tank",vdev="mirror-3",guid="{TANK_GUID_BAY3}",'
        f'name="{UUID_BAY3}",device="sdd",bay="3",serial="IB24AK0004S00064"}} 2'
    ], bad
    good = vdev(out, "zfs_vdev_state", TANK_GUID_BAY5)
    assert len(good) == 1 and 'bay="5"' in good[0] and good[0].endswith(" 0"), good
    assert vdev(out, "zfs_vdev_write_errors", TANK_GUID_BAY3)[0].endswith(" 256")
    assert vdev(out, "zfs_vdev_read_errors", TANK_GUID_BAY3)[0].endswith(" 3")
    assert "zfs_vdev_scrape_ok 1" in out.splitlines()


def test_a_vanished_disk_keeps_its_last_known_bay_and_serial():
    """When the drive drops off the bus its by-partuuid link and sysfs node go
    away, which is exactly when the alert most needs to name the tray."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_disk(root, "sdd", 3, "IB24AK0004S00064")
        link = partuuid_link(root, UUID_BAY3, "sdd1")
        h = VdevHarness(tmp, status_json([leaf(TANK_GUID_BAY3, UUID_BAY3, link, "ONLINE")]))
        first = h.run()
        assert 'bay="3"' in vdev(first, "zfs_vdev_state", TANK_GUID_BAY3)[0]

        # The disk detaches: link and sysfs entries are gone, ZFS says REMOVED.
        link.unlink()
        for p in ("sdd", "sdd1"):
            (root / "sys" / "class" / "block" / p).unlink()
        h.set_status(status_json([leaf(TANK_GUID_BAY3, UUID_BAY3, link, "REMOVED")]))
        second = h.run()

    line = vdev(second, "zfs_vdev_state", TANK_GUID_BAY3)
    assert len(line) == 1, line
    assert 'device="unknown"' in line[0]
    assert 'bay="3"' in line[0] and 'serial="IB24AK0004S00064"' in line[0], line[0]
    assert line[0].endswith(" 5"), line[0]


def test_cache_is_not_left_where_node_exporter_would_parse_it():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_disk(root, "sdd", 3, "IB24AK0004S00064")
        h = VdevHarness(tmp, status_json([
            leaf(TANK_GUID_BAY3, UUID_BAY3, partuuid_link(root, UUID_BAY3, "sdd1"), "ONLINE")]))
        h.run()
        names = sorted(p.name for p in h.out_dir.iterdir())
    assert names == [".zfs_vdev_labels", "zfs_smart.prom"], names


@pytest.mark.parametrize("doc,rc", [(None, 0), ({"broken": True}, 0), (status_json([]), 1)])
def test_unreadable_zpool_status_says_so_instead_of_going_quiet(doc, rc):
    """No vdev series at all must be distinguishable from all vdevs healthy."""
    with TemporaryDirectory() as tmp:
        out = VdevHarness(tmp, doc, zpool_rc=rc).run()
    assert "zfs_vdev_scrape_ok 0" in out.splitlines()
    assert not samples(out, "zfs_vdev_state")


def test_an_available_spare_is_not_reported_as_a_fault():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_disk(root, "sdd", 3, "IB24AK0004S00064")
        fake_disk(root, "sde", 4, "SPARE0001")
        spare = leaf("111", "spare-uuid", partuuid_link(root, "spare-uuid", "sde1"), "AVAIL")
        spare["class"] = "spare"
        doc = status_json(
            [leaf(TANK_GUID_BAY3, UUID_BAY3, partuuid_link(root, UUID_BAY3, "sdd1"), "ONLINE")],
            spares={"spare-uuid": spare},
        )
        out = VdevHarness(tmp, doc).run()
    line = vdev(out, "zfs_vdev_state", "111")
    assert len(line) == 1 and 'vdev="spare"' in line[0] and line[0].endswith(" 0"), line


def test_unreadable_disk_scrape_ok_still_names_bay_and_serial():
    """smart_scrape_ok is what fired on 2026-10-08, labelled only device="sdd".
    smartctl cannot read a serial from a dead drive, so it must come from the
    serial the kernel cached at attach time."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        # The Harness globs <tmp>/dev/sd?, and its fake node is named sda.
        fake_disk(root, "sda", 3, "IB24AK0004S00064")
        h = VdevHarness(tmp, status_json([]))
        (root / "bin" / "smartctl").write_text("#!/bin/sh\nexit 2\n")
        out = h.run()
    ok = samples(out, "smart_scrape_ok")
    assert ok == ['smart_scrape_ok{device="sda",bay="3",serial="IB24AK0004S00064"} 0'], ok


def test_healthy_smart_series_carry_the_bay():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_disk(root, "sda", 6, "24230KD00111")
        out = VdevHarness(tmp, status_json([])).run()
    health = samples(out, "smart_device_health")
    assert len(health) == 1 and 'bay="6"' in health[0], health
