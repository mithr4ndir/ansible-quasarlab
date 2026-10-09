"""Runs the memory-sync bash suite under pytest, so it is part of the normal run.

memory_sync_test.sh drives files/memory-sync.sh against real git repositories
in a temp dir, with gh and docker stubbed. It asserts that only memory/ is ever
committed, that a secret finding or a missing scanner blocks the push, that
deletions propagate without deleting files another clone added, and that the
live checkout's uncommitted non-memory work is never touched.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SUITE = Path(__file__).with_name("memory_sync_test.sh")


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_memory_sync_suite_passes():
    proc = subprocess.run(["bash", str(SUITE)], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Guard against a suite that silently ran nothing.
    assert " passed, 0 failed" in proc.stdout, proc.stdout
    assert int(proc.stdout.rsplit(" passed", 1)[0].split()[-1]) >= 20, proc.stdout
