#!/usr/bin/env python3
"""Disable the UGREEN/ITE IT8613 Super I/O watchdog.

UGREEN firmware arms a 180s watchdog that resets the box when its stock UGOS is
not detected. This machine runs TrueNAS, so nothing services it and the box
hard-resets every ~3 minutes. Super I/O registers are volatile and BIOS
re-arms the timer on every boot, so this must run each boot.

Proper fix is to disable Watchdog Timer in the BIOS; this is the remote workaround.
"""
import os, sys

BASE, DATA, LDN_WDT = 0x2e, 0x2f, 0x07
EXPECTED_CHIP = 0x8613

fd = os.open("/dev/port", os.O_RDWR)
outb = lambda v, p: os.pwrite(fd, bytes([v]), p)
inb  = lambda p: os.pread(fd, 1, p)[0]

def enter(): [outb(b, BASE) for b in (0x87, 0x01, 0x55, 0x55)]
def leave(): outb(0x02, BASE); outb(0x02, DATA)
def rd(r):   outb(r, BASE); return inb(DATA)
def wr(r,v): outb(r, BASE); outb(v, DATA)

enter()
chip = (rd(0x20) << 8) | rd(0x21)
if chip != EXPECTED_CHIP:
    leave()
    sys.exit(f"refusing to write: expected chip 0x{EXPECTED_CHIP:04x}, found 0x{chip:04x}")

outb(0x07, BASE); outb(LDN_WDT, DATA)
before = rd(0x73)
wr(0x73, 0x00)
wr(0x74, 0x00)
after = rd(0x73)
leave()
print(f"ugreen-wdt: chip 0x{chip:04x} timeout 0x{before:02x} -> 0x{after:02x}")
sys.exit(0 if after == 0 else "FAILED: timeout register did not clear")
