"""Behaviour tests for the UGREEN IT8613 watchdog disable.

The script writes to /dev/port, so it cannot be run as-is in a test. It is
also the single most dangerous file in this repo to get wrong: if it stops
disarming the watchdog the NAS hard-resets every ~3 minutes, which is the
15 hour full-lab outage of 2026-09-20.

So it is deliberately NOT modified to add a test hook. Instead these tests
execute the real source with a stub `os` module in place, which exercises the
actual Super I/O index/data protocol the script speaks: the unlock sequence,
the chip-ID read, the logical-device select, and the register writes. Nothing
about the script changes to make it testable.

The case that matters most is the chip guard. These registers are a generic
Super I/O interface and the same addresses exist on other boards, so writing
0x00 to 0x73 without first confirming the chip is an IT8613 would poke an
arbitrary register on unknown hardware.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "files" / "ugreen-wdt-disable.py"

BASE, DATA = 0x2E, 0x2F
UNLOCK = [0x87, 0x01, 0x55, 0x55]

# A stub Super I/O that speaks the index/data protocol over a fake /dev/port.
# Config registers 0x20/0x21 hold the chip ID and are not per logical device;
# everything else is addressed within the currently selected LDN, which is
# itself selected by writing the LDN to register 0x07.
HARNESS = textwrap.dedent(
    """
    import json, sys, types

    chip = CHIP
    sticky = STICKY          # registers that refuse to change (fake hardware fault)
    regs = {(0x07, 0x73): 0xb4, (0x07, 0x74): 0x12}
    config = {0x20: (chip >> 8) & 0xFF, 0x21: chip & 0xFF}
    state = {"index": 0, "ldn": 0, "unlock": []}
    writes = []

    def _pwrite(fd, data, port):
        v = data[0]
        if port == 0x2E:
            state["unlock"].append(v)
            state["index"] = v
        elif port == 0x2F:
            idx = state["index"]
            if idx == 0x07:
                state["ldn"] = v
            else:
                writes.append((state["ldn"], idx, v))
                if (state["ldn"], idx) not in sticky:
                    regs[(state["ldn"], idx)] = v
        return 1

    def _pread(fd, n, port):
        idx = state["index"]
        if idx in config:
            return bytes([config[idx]])
        return bytes([regs.get((state["ldn"], idx), 0)])

    fake = types.ModuleType("os")
    fake.open = lambda path, flags: 3
    fake.O_RDWR = 2
    fake.pwrite = _pwrite
    fake.pread = _pread
    sys.modules["os"] = fake

    src = open(SCRIPT_PATH).read()
    code = compile(src, SCRIPT_PATH, "exec")
    rc = 0
    try:
        exec(code, {"__name__": "__main__"})
    except SystemExit as e:
        rc = e.code if e.code is not None else 0
    sys.stderr.write(json.dumps({
        "rc": 0 if rc == 0 else 1,
        "rc_raw": str(rc),
        "writes": writes,
        "regs": {f"{l:02x}:{r:02x}": v for (l, r), v in regs.items()},
        "unlock": state["unlock"][:4],
    }))
    """
)


def run(chip=0x8613, sticky=()):
    src = (
        HARNESS.replace("CHIP", hex(chip))
        .replace("STICKY", repr(set(sticky)))
        .replace("SCRIPT_PATH", repr(str(SCRIPT)))
    )
    p = subprocess.run([sys.executable, "-I", "-c", src], capture_output=True, text=True)
    import json

    # The harness prints its report to stderr; the script's own print goes to stdout.
    report = json.loads(p.stderr[p.stderr.index("{") :])
    report["stdout"] = p.stdout
    return report


def test_the_real_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111, "role copies it as 0700, it must be executable"


def test_it_disarms_the_watchdog_on_the_expected_chip():
    r = run(chip=0x8613)
    assert r["rc"] == 0, r
    assert r["regs"]["07:73"] == 0, "timeout register must be zeroed"
    assert r["regs"]["07:74"] == 0, "both timeout bytes must be zeroed"


def test_it_selects_the_watchdog_logical_device_before_writing():
    r = run(chip=0x8613)
    assert all(ldn == 0x07 for ldn, _, _ in r["writes"]), (
        f"writes landed outside LDN 7: {r['writes']}"
    )


def test_it_sends_the_unlock_sequence_first():
    r = run(chip=0x8613)
    assert r["unlock"] == UNLOCK


def test_it_refuses_to_write_to_an_unexpected_chip():
    """The guard that stops this poking arbitrary registers on other hardware.

    0x8728 is a real ITE part, so this is not a far-fetched input.
    """
    r = run(chip=0x8728)
    assert r["rc"] != 0, "must refuse"
    # leave() still writes the exit-config-mode register, which is correct and
    # harmless. What must not happen is a write to the watchdog timeout bytes.
    touched = [(l, reg) for l, reg, _ in r["writes"] if reg in (0x73, 0x74)]
    assert touched == [], f"wrote to the timeout registers anyway: {r['writes']}"
    assert r["regs"]["07:73"] == 0xB4, "timeout must be left untouched"
    assert r["regs"]["07:74"] == 0x12, "second timeout byte must be left untouched"
    assert "8613" in r["rc_raw"] and "8728" in r["rc_raw"], (
        f"the refusal must name both chip IDs, got: {r['rc_raw']}"
    )


def test_it_fails_loudly_when_the_register_will_not_clear():
    """Hardware that accepts the write and keeps its value. Without the
    read-back the script would report success while the box stays armed, which
    is the same shape as every other green-but-blind failure in this lab."""
    r = run(chip=0x8613, sticky=[(0x07, 0x73)])
    assert r["rc"] != 0, "a register that did not clear must be an error"
    assert "FAIL" in r["rc_raw"].upper(), r["rc_raw"]
