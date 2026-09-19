"""Tests for files/kuma-gate.js against fixtures/fake-kuma.js.

Run from the repo root, with socket.io and socket.io-client 4.8 installed in
some node_modules directory (see test_kuma_admin.py):

    KUMA_BOOTSTRAP_NODE_MODULES=/tmp/kuma-node/node_modules \\
        uv run --python 3.12 --with pytest==8.4.2 pytest roles/uptime_kuma/tests -rs

The gate decides whether the TLS proxy may forward anything to Kuma. What is
pinned here: it stays closed until Kuma has our admin account, closes at once
when Kuma goes away, and stays closed when Kuma comes back needing setup (a
lost or wiped database) or owned by someone else. The lost-database case is
the one a deploy-time check cannot cover: the stack is already running on the
LAN when it happens.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

ROLE = Path(__file__).resolve().parents[1]
GATE = ROLE / "files" / "kuma-gate.js"
ADMIN = ROLE / "files" / "kuma-admin.js"
FAKE = Path(__file__).resolve().parent / "fixtures" / "fake-kuma.js"
MODULES = os.environ.get("KUMA_BOOTSTRAP_NODE_MODULES", "")
PASSWORD = "correct-horse-battery-staple-0123456789"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (Path(MODULES) / "socket.io-client").is_dir(),
    reason="node and KUMA_BOOTSTRAP_NODE_MODULES (socket.io + socket.io-client) not available")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Kuma:
    """fake-kuma.js on a fixed port, so it can be killed and started again."""

    def __init__(self) -> None:
        self.port = free_port()
        self.proc: subprocess.Popen | None = None

    def start(self, mode: str) -> None:
        self.proc = subprocess.Popen(["node", str(FAKE), mode, str(self.port)],
                                     env={**os.environ, "NODE_PATH": MODULES},
                                     stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout is not None and self.proc.stdout.readline().strip() == "listening"

    def stop(self) -> None:
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None


@pytest.fixture
def kuma() -> Iterator[Kuma]:
    k = Kuma()
    yield k
    k.stop()


@pytest.fixture
def gate(kuma: Kuma, tmp_path: Path) -> Iterator[int]:
    (tmp_path / "pw").write_text(PASSWORD + "\n")
    port = free_port()
    proc = subprocess.Popen(
        ["node", str(GATE)],
        env={**os.environ, "NODE_PATH": MODULES, "KUMA_URL": f"http://127.0.0.1:{kuma.port}",
             "KUMA_ADMIN_PASSWORD_FILE": str(tmp_path / "pw"), "KUMA_GATE_PORT": str(port),
             "KUMA_GATE_CHECK_MS": "500", "KUMA_GATE_ANSWER_MS": "2000"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            state(port)
            break
        except OSError:
            time.sleep(0.1)
    yield port
    proc.kill()
    out = proc.communicate()[0]
    assert PASSWORD not in out


def state(port: int) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/gate", timeout=3) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def wait_for(port: int, want: int, seconds: float) -> float:
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        if state(port) == want:
            return time.monotonic() - start
        time.sleep(0.1)
    raise AssertionError(f"gate did not become {want} within {seconds}s (still {state(port)})")


def stays(port: int, want: int, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        assert state(port) == want
        time.sleep(0.1)


def bootstrap(kuma: Kuma, tmp_path: Path) -> None:
    (tmp_path / "pw").write_text(PASSWORD + "\n")
    env = {**os.environ, "NODE_PATH": MODULES, "KUMA_URL": f"http://127.0.0.1:{kuma.port}",
           "KUMA_ADMIN_USERNAME": "admin", "KUMA_ADMIN_PASSWORD_FILE": str(tmp_path / "pw"),
           "KUMA_ACTION": "bootstrap"}
    with ADMIN.open("rb") as stdin:
        proc = subprocess.run(["node", "-"], stdin=stdin, env=env, capture_output=True,
                              text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_closed_while_kuma_needs_setup_and_open_once_our_admin_exists(
        kuma: Kuma, gate: int, tmp_path: Path) -> None:
    kuma.start("fresh")
    stays(gate, 403, 3.0)
    bootstrap(kuma, tmp_path)
    wait_for(gate, 204, 5.0)


def test_lost_database_while_running_on_the_lan_keeps_the_gate_closed(
        kuma: Kuma, gate: int, tmp_path: Path) -> None:
    # Running and open, as after a normal deploy.
    kuma.start("fresh")
    bootstrap(kuma, tmp_path)
    wait_for(gate, 204, 5.0)
    # The database is lost and Kuma restarts on an empty one: needSetup is
    # true again. With the old deploy-time marker the port stayed on the LAN
    # and the first visitor would become admin. The gate must close when
    # Kuma goes away and must not reopen for a Kuma that needs setup.
    kuma.stop()
    closed_after = wait_for(gate, 403, 2.0)
    assert closed_after < 2.0
    kuma.start("fresh")
    stays(gate, 403, 5.0)
    # Only after our own admin is back does it reopen.
    bootstrap(kuma, tmp_path)
    wait_for(gate, 204, 5.0)


def test_kuma_owned_by_someone_else_never_opens(kuma: Kuma, gate: int) -> None:
    kuma.start("owned")
    stays(gate, 403, 5.0)


def test_kuma_down_is_closed(kuma: Kuma, gate: int) -> None:
    stays(gate, 403, 2.0)


def test_kuma_that_never_answers_is_closed(kuma: Kuma, gate: int) -> None:
    kuma.start("silent")
    stays(gate, 403, 4.0)
