"""Tests for files/kuma-admin.js against fixtures/fake-kuma.js.

Run from the repo root, with socket.io and socket.io-client 4.8 installed in
some node_modules directory (Kuma 2.5.5 ships socket.io-client ~4.8.3):

    npm install --prefix /tmp/kuma-node socket.io@4.8.3 socket.io-client@4.8.3
    KUMA_BOOTSTRAP_NODE_MODULES=/tmp/kuma-node/node_modules \
        uv run --python 3.12 --with pytest==8.4.2 pytest roles/uptime_kuma/tests -rs

Without node or those modules the tests skip, and -rs says so. The fake
implements the three socket.io events the script uses with the contracts of
Kuma's server/server.js; it is not Kuma. What is pinned here is the script's
own behaviour: create the admin only when none exists, always prove the login,
refuse a Kuma someone else set up, give up at the deadline, never print the
password, and list entities without their URLs, tokens or configs. The fake
registers its handlers only after a delay and an "info" event, the way Kuma's
connection handler does; a script that asks before "info" loses its first
event. That race was hit for real against a fresh Kuma 2.5.5 on 2026-09-19.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

ROLE = Path(__file__).resolve().parents[1]
SCRIPT = ROLE / "files" / "kuma-admin.js"
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


@pytest.fixture
def kuma(request: pytest.FixtureRequest) -> int:
    port = free_port()
    proc = subprocess.Popen(["node", str(FAKE), request.param, str(port)],
                            env={**os.environ, "NODE_PATH": MODULES},
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stdout.readline().strip() == "listening"
    yield port
    proc.kill()
    proc.wait()


def bootstrap(port: int, tmp_path: Path, password: str | None = PASSWORD,
              username: str = "admin", timeout_ms: int = 30000,
              action: str = "bootstrap") -> tuple[int, dict, str, float]:
    pw_file = tmp_path / "admin_password"
    if password is not None:
        pw_file.write_text(password + "\n")
    env = {**os.environ, "NODE_PATH": MODULES, "KUMA_URL": f"http://127.0.0.1:{port}",
           "KUMA_ADMIN_USERNAME": username, "KUMA_ADMIN_PASSWORD_FILE": str(pw_file),
           "KUMA_ADMIN_TIMEOUT_MS": str(timeout_ms), "KUMA_ACTION": action}
    start = time.monotonic()
    with SCRIPT.open("rb") as stdin:
        proc = subprocess.run(["node", "-"], stdin=stdin, env=env, capture_output=True,
                              text=True, timeout=60, check=False)
    elapsed = time.monotonic() - start
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, proc.stdout + proc.stderr
    return proc.returncode, json.loads(lines[0]), proc.stdout + proc.stderr, elapsed


@pytest.mark.parametrize("kuma", ["fresh"], indirect=True)
def test_fresh_kuma_gets_our_admin_and_a_rerun_changes_nothing(kuma: int, tmp_path: Path) -> None:
    rc, out, text, _ = bootstrap(kuma, tmp_path)
    assert (rc, out) == (0, {"ok": True, "created": True})
    rc, out, _, _ = bootstrap(kuma, tmp_path)
    assert (rc, out) == (0, {"ok": True, "created": False})
    assert PASSWORD not in text


@pytest.mark.parametrize("kuma", ["owned"], indirect=True)
def test_kuma_set_up_by_someone_else_fails_the_deploy(kuma: int, tmp_path: Path) -> None:
    rc, out, text, _ = bootstrap(kuma, tmp_path)
    assert rc == 5
    assert out["ok"] is False and out["created"] is False
    assert "login refused" in out["error"]
    assert PASSWORD not in text


@pytest.mark.parametrize("kuma", ["silent"], indirect=True)
def test_a_kuma_that_never_answers_hits_the_deadline(kuma: int, tmp_path: Path) -> None:
    rc, out, _, elapsed = bootstrap(kuma, tmp_path, timeout_ms=2000)
    assert rc == 3
    assert "no answer from Kuma within 2000 ms" in out["error"]
    assert elapsed < 6


def test_nothing_listening_is_a_connect_error(tmp_path: Path) -> None:
    rc, out, _, elapsed = bootstrap(free_port(), tmp_path)
    assert rc == 3 and out["error"].startswith("connect_error")
    assert elapsed < 15


@pytest.mark.parametrize("password, username", [
    (None, "admin"),                # file missing
    ("x7-pw", "admin"),             # below 16 characters
    (PASSWORD, "a b;c"),            # username outside the allowlist
])
def test_bad_input_is_refused_before_connecting(password: str | None, username: str,
                                                tmp_path: Path) -> None:
    rc, out, text, _ = bootstrap(free_port(), tmp_path, password=password, username=username)
    assert rc == 2 and out["ok"] is False
    if password:
        assert password not in text


@pytest.mark.parametrize("kuma", ["fresh"], indirect=True)
def test_list_reports_names_and_wiring_but_no_secrets(kuma: int, tmp_path: Path) -> None:
    assert bootstrap(kuma, tmp_path)[0] == 0
    rc, out, text, _ = bootstrap(kuma, tmp_path, action="list")
    assert rc == 0 and out["ok"] is True
    assert out["monitors"] == [
        {"name": "Prometheus", "active": True, "notificationIDList": {"1": True}},
        {"name": "NFS", "active": True, "notificationIDList": {"1": True}},
    ]
    assert out["notifications"] == [{"id": 1, "name": "Discord", "active": True}]
    for secret in ("SECRET", "http://192.0.2.1", PASSWORD):
        assert secret not in text


@pytest.mark.parametrize("kuma", ["owned"], indirect=True)
def test_list_with_the_wrong_password_is_refused(kuma: int, tmp_path: Path) -> None:
    rc, out, _, _ = bootstrap(kuma, tmp_path, action="list")
    assert rc == 5 and out["ok"] is False


def test_unknown_action_is_refused(tmp_path: Path) -> None:
    rc, out, _, _ = bootstrap(free_port(), tmp_path, action="rm-rf")
    assert rc == 2 and "KUMA_ACTION" in out["error"]
