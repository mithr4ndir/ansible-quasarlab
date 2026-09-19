"""Tests for files/kuma-nfs-probe.

Run from the repo root:
    uv run --python 3.12 --with pytest==8.4.2 --with ansible-core==2.16.3 \
        pytest roles/uptime_kuma/tests -rs

Nothing here touches a real NAS or a real Kuma. The push endpoint is a local
HTTP server, and NFS is either a fake nfs-cat script or the real libnfs client
talking to fake_nfs_server.py on 127.0.0.1.

What matters most is the time bound. A probe that hangs is worse than no
probe: it looks like a monitor and reports nothing. So the hang tests run a
child that really does not return (it ignores SIGTERM, or it leaves a
grandchild holding the pipes, or it is the real nfs-cat blocked on a server
that accepted the connection and never answers) and assert wall-clock time,
exit status, the DOWN push, and that no child is left running.

The real-libnfs tests need nfs-cat and nfs-cp. They use /usr/bin/nfs-cat when
libnfs-utils is installed, or KUMA_PROBE_LIBNFS_ROOT pointing at an extracted
libnfs-utils + libnfs13 package tree (the directory holding usr/bin and
usr/lib). Otherwise they skip, and -rs says so.
"""

from __future__ import annotations

import ast
import http.server
import importlib.machinery
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_nfs_server import FakeNfsServer, State, blackhole_listener  # noqa: E402

ROLE = Path(__file__).resolve().parents[1]
PROBE = ROLE / "files" / "kuma-nfs-probe"
TOKEN = "Tok3nTok3nTok3nTok3nTok3nTok3n12"
EXPECTED = b"uptime-kuma nfs probe sentinel v1\n"
EXPORT = "/mnt/tank/k8s"
SENTINEL = ".uptime-kuma-nfs-probe"


def load_probe() -> Any:
    loader = importlib.machinery.SourceFileLoader("kuma_nfs_probe", str(PROBE))
    spec = importlib.util.spec_from_loader("kuma_nfs_probe", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules.
    sys.modules["kuma_nfs_probe"] = module
    loader.exec_module(module)
    return module


probe = load_probe()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class PushRecorder(http.server.BaseHTTPRequestHandler):
    """Stands in for Kuma's /api/push/<token>."""

    def do_GET(self) -> None:  # noqa: N802
        server: Any = self.server
        parsed = urllib.parse.urlparse(self.path)
        token = parsed.path.rsplit("/", 1)[-1]
        server.requests.append({"token": token, **dict(urllib.parse.parse_qsl(parsed.query))})
        if server.mode == "hang":
            server.release.wait()
            return
        ok = server.mode == "ok" and token == TOKEN
        body = json.dumps({"ok": ok} if ok else {"ok": False, "msg": "Monitor not found or not active."})
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def kuma() -> Iterator[Any]:
    server: Any = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PushRecorder)
    server.daemon_threads = True
    server.requests = []
    server.mode = "ok"
    server.release = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base = f"http://127.0.0.1:{server.server_address[1]}/api/push"
    yield server
    server.release.set()
    server.shutdown()
    server.server_close()


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    token = tmp_path / "push_token"
    token.write_text(TOKEN + "\n")
    expected = tmp_path / "expected"
    expected.write_bytes(EXPECTED)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    return {"token": token, "expected": expected, "bin": bindir, "tmp": tmp_path}


def fake_exe(files: dict[str, Path], name: str, body: str) -> Path:
    path = files["bin"] / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)
    return path


def args_for(files: dict[str, Path], kuma: Any, mode: str = "probe", timeout: float = 5.0,
             nfs_cat: Path | str | None = None, nfs_cp: Path | str | None = None,
             extra: list[str] | None = None) -> list[str]:
    return [
        mode, "--server", "192.0.2.15", "--export", EXPORT, "--sentinel", SENTINEL,
        "--expected-file", str(files["expected"]), "--timeout", str(timeout),
        "--nfs-cat", str(nfs_cat or files["bin"] / "nfs-cat"),
        "--nfs-cp", str(nfs_cp or files["bin"] / "nfs-cp"),
        "--push-base", kuma.base, "--push-token-file", str(files["token"]),
        *(extra or []),
    ]


# No single call in these tests may take longer than this. A probe that has
# lost its deadline then fails the test instead of hanging the test run.
HARD_LIMIT_SECS = 30.0


def bounded_call(fn: Any, *args: Any, limit: float = HARD_LIMIT_SECS, **kwargs: Any) -> Any:
    result: dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # re-raised in the test thread
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(limit)
    if thread.is_alive():
        pytest.fail(f"{getattr(fn, '__name__', fn)} still running after {limit:g}s: "
                    "the time bound is broken")
    if "error" in result:
        raise result["error"]
    return result["value"]


def run_main(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict, float]:
    start = time.monotonic()
    rc = bounded_call(probe.main, argv)
    elapsed = time.monotonic() - start
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1, out
    return rc, json.loads(out[0]), elapsed


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is dead for our purposes.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Behaviour with a fake nfs-cat
# ---------------------------------------------------------------------------

def test_good_read_pushes_up(files: dict[str, Path], kuma: Any,
                             capsys: pytest.CaptureFixture[str]) -> None:
    fake_exe(files, "nfs-cat", f"printf '%s' '{EXPECTED.decode()}'\n")
    rc, event, _ = run_main(args_for(files, kuma), capsys)
    assert rc == probe.EXIT_OK
    assert event["ok"] is True and event["push"] == "accepted"
    [req] = kuma.requests
    assert req["token"] == TOKEN
    assert req["status"] == "up"
    assert float(req["ping"]) >= 0
    assert "192.0.2.15:/mnt/tank/k8s/.uptime-kuma-nfs-probe" in req["msg"]


def test_url_carries_version_and_identity(files: dict[str, Path], kuma: Any,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    record = files["tmp"] / "argv"
    fake_exe(files, "nfs-cat", f"printf '%s\\n' \"$@\" > {record}\nprintf '%s' '{EXPECTED.decode()}'\n")
    run_main(args_for(files, kuma, extra=["--uid", "1000", "--gid", "1001"]), capsys)
    assert record.read_text().splitlines() == [
        "nfs://192.0.2.15/mnt/tank/k8s/.uptime-kuma-nfs-probe?version=3&uid=1000&gid=1001"]


@pytest.mark.parametrize("script, reason", [
    ("echo 'Failed to mount nfs share : RPC error: Mount failed with error "
     "MNT3ERR_ACCES(13)' >&2\nexit 10\n", "MNT3ERR_ACCES"),
    ("printf 'somebody else wrote this\\n'\n", "unexpected content"),
    ("head -c 5000 /dev/zero\n", "unexpected content"),
    ("exit 10\n", "nfs-cat exited 10"),
])
def test_failed_or_wrong_read_pushes_down(script: str, reason: str, files: dict[str, Path],
                                          kuma: Any, capsys: pytest.CaptureFixture[str]) -> None:
    fake_exe(files, "nfs-cat", script)
    rc, event, _ = run_main(args_for(files, kuma), capsys)
    assert rc == probe.EXIT_NFS_FAILED
    [req] = kuma.requests
    assert req["status"] == "down"
    assert "ping" not in req
    assert reason in req["msg"] and reason in event["msg"]


def test_missing_client_binary_pushes_down_instead_of_crashing(
        files: dict[str, Path], kuma: Any, capsys: pytest.CaptureFixture[str]) -> None:
    rc, event, _ = run_main(args_for(files, kuma, nfs_cat=files["bin"] / "absent"), capsys)
    assert rc == probe.EXIT_NFS_FAILED
    assert "cannot execute absent" in event["msg"]
    assert kuma.requests[0]["status"] == "down"


def test_hung_read_is_killed_at_the_deadline(files: dict[str, Path], kuma: Any,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    # Ignores SIGTERM and never finishes: only SIGKILL of the group ends it.
    pidfile = files["tmp"] / "pid"
    fake_exe(files, "nfs-cat", f"trap '' TERM\necho $$ > {pidfile}\nfor _ in $(seq 60); do sleep 1; done\n")
    rc, event, elapsed = run_main(args_for(files, kuma, timeout=1.5), capsys)
    assert rc == probe.EXIT_NFS_FAILED
    assert 1.5 <= elapsed < 1.5 + 2.0, elapsed
    assert "did not finish within 1.5s" in event["msg"]
    assert kuma.requests[0]["status"] == "down"
    assert not pid_alive(int(pidfile.read_text()))


def test_group_kill_takes_the_grandchildren_too(files: dict[str, Path]) -> None:
    pidfile = files["tmp"] / "grandchild"
    exe = fake_exe(files, "nfs-cat", f"sleep 120 &\necho $! > {pidfile}\nwait\n")
    result = bounded_call(probe.run_bounded, [str(exe)], timeout=1.0, kill_grace=1.0)
    assert result.timed_out
    assert result.elapsed < 1.0 + 2.0
    assert not pid_alive(int(pidfile.read_text()))


def test_escaped_grandchild_holding_the_pipes_cannot_stretch_the_deadline(
        files: dict[str, Path]) -> None:
    # The grandchild leaves the process group (setsid) but keeps stdout and
    # stderr open, so killing the group does not close them and a plain
    # communicate() would block until it exits. The bound must still hold.
    pidfile = files["tmp"] / "escapee"
    exe = fake_exe(files, "nfs-cat", f"setsid sleep 120 &\necho $! > {pidfile}\nwait\n")
    try:
        result = bounded_call(probe.run_bounded, [str(exe)], timeout=1.0, kill_grace=0.5)
        assert result.timed_out and result.returncode is None
        assert result.elapsed < 1.0 + 2 * 0.5 + 0.5, result.elapsed
    finally:
        os.kill(int(pidfile.read_text()), signal.SIGKILL)


def test_a_hung_push_endpoint_is_bounded_too(files: dict[str, Path], kuma: Any,
                                             capsys: pytest.CaptureFixture[str],
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    fake_exe(files, "nfs-cat", f"printf '%s' '{EXPECTED.decode()}'\n")
    monkeypatch.setattr(probe, "PUSH_TIMEOUT_SECS", 1.0)
    kuma.mode = "hang"
    rc, event, elapsed = run_main(args_for(files, kuma), capsys)
    assert rc == probe.EXIT_PUSH_FAILED
    assert elapsed < 3.0
    assert event["push"].startswith("push endpoint unreachable")


@pytest.mark.parametrize("mode, reason", [
    ("reject", "push endpoint answered HTTP 404"),
])
def test_rejected_push_is_exit_2(mode: str, reason: str, files: dict[str, Path], kuma: Any,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    fake_exe(files, "nfs-cat", f"printf '%s' '{EXPECTED.decode()}'\n")
    kuma.mode = mode
    rc, event, _ = run_main(args_for(files, kuma), capsys)
    assert rc == probe.EXIT_PUSH_FAILED
    assert event["push"] == reason


def test_unreachable_push_endpoint_is_exit_2(files: dict[str, Path], kuma: Any,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    fake_exe(files, "nfs-cat", f"printf '%s' '{EXPECTED.decode()}'\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    argv = args_for(files, kuma)
    argv[argv.index("--push-base") + 1] = f"http://127.0.0.1:{closed_port}/api/push"
    rc, event, _ = run_main(argv, capsys)
    assert rc == probe.EXIT_PUSH_FAILED
    assert event["push"] == "push endpoint unreachable (URLError)"


def test_nfs_failure_wins_over_push_failure(files: dict[str, Path], kuma: Any,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    fake_exe(files, "nfs-cat", "exit 10\n")
    kuma.mode = "reject"
    rc, _, _ = run_main(args_for(files, kuma), capsys)
    assert rc == probe.EXIT_NFS_FAILED


def test_token_never_reaches_the_log(files: dict[str, Path], kuma: Any,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    fake_exe(files, "nfs-cat", "echo boom >&2; exit 10\n")
    kuma.mode = "reject"
    bounded_call(probe.main, args_for(files, kuma))
    fake_exe(files, "nfs-cat", f"printf '%s' '{EXPECTED.decode()}'\n")
    kuma.mode = "ok"
    bounded_call(probe.main, args_for(files, kuma))
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


@pytest.mark.parametrize("flag, value", [
    ("--server", "nas;rm -rf /"),
    ("--server", "-oProxyCommand=x"),
    ("--export", "mnt/tank"),
    ("--export", "/mnt/../etc"),
    ("--export", "/mnt/tank?uid=0"),
    ("--sentinel", "../passwd"),
    ("--sentinel", "a/b"),
    ("--sentinel", ".."),
    ("--nfs-version", "2"),
    ("--timeout", "0"),
    ("--timeout", "1000"),
    ("--uid", "-1"),
    ("--nfs-port", "70000"),
    ("--push-base", "http://127.0.0.1:3001/api/push/../../x"),
    ("--push-base", "file:///etc/passwd"),
])
def test_bad_config_is_refused_before_anything_runs(flag: str, value: str, files: dict[str, Path],
                                                    kuma: Any,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    marker = files["tmp"] / "ran"
    fake_exe(files, "nfs-cat", f"touch {marker}\n")
    argv = args_for(files, kuma)
    if flag in argv:
        i = argv.index(flag)
        del argv[i:i + 2]
    # flag=value form, so values that start with "-" reach the validation.
    argv.append(f"{flag}={value}")
    rc, event, _ = run_main(argv, capsys)
    assert rc == probe.EXIT_CONFIG
    assert event["event"] == "config_error"
    assert not marker.exists()
    assert kuma.requests == []


@pytest.mark.parametrize("content", ["", "short", TOKEN + "!", "x" * 33, "tok en" * 6])
def test_bad_token_file_is_refused_without_echoing_it(content: str, files: dict[str, Path],
                                                      kuma: Any,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    files["token"].write_text(content)
    fake_exe(files, "nfs-cat", f"printf '%s' '{EXPECTED.decode()}'\n")
    rc, event, _ = run_main(args_for(files, kuma), capsys)
    assert rc == probe.EXIT_CONFIG
    if content:
        assert content not in event["error"]
    assert kuma.requests == []


def test_token_defaults_to_the_systemd_credential(files: dict[str, Path], kuma: Any,
                                                  capsys: pytest.CaptureFixture[str],
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    fake_exe(files, "nfs-cat", f"printf '%s' '{EXPECTED.decode()}'\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(files["tmp"]))
    argv = args_for(files, kuma)
    i = argv.index("--push-token-file")
    del argv[i:i + 2]
    rc, _, _ = run_main(argv, capsys)
    assert rc == probe.EXIT_OK
    assert kuma.requests[0]["token"] == TOKEN


# ---------------------------------------------------------------------------
# ensure-sentinel with fakes
# ---------------------------------------------------------------------------

def fake_store(files: dict[str, Path], initial: bytes | None, cat_extra: str = "") -> Path:
    """nfs-cat/nfs-cp fakes backed by one local file standing in for the NAS."""
    store = files["tmp"] / "store"
    if initial is not None:
        store.write_bytes(initial)
    cplog = files["tmp"] / "cp-calls"
    fake_exe(files, "nfs-cat", cat_extra + f"""
if [[ -f {store} ]]; then cat {store}; exit 0; fi
echo 'Failed to open file /x: open call failed with "NFS: Lookup of /x failed with NFS3ERR_NOENT(-2)"' >&2
exit 10
""")
    fake_exe(files, "nfs-cp", f"""
echo "$@" >> {cplog}
[[ -e {store} ]] && {{ echo 'Failed to creat file: NFS3ERR_EXIST' >&2; exit 10; }}
cp "$1" {store}
""")
    return cplog


def test_ensure_sentinel_leaves_an_existing_sentinel_alone(
        files: dict[str, Path], kuma: Any, capsys: pytest.CaptureFixture[str]) -> None:
    cplog = fake_store(files, EXPECTED)
    rc, event, _ = run_main(args_for(files, kuma, mode="ensure-sentinel"), capsys)
    assert rc == probe.EXIT_OK and event["ok"] is True
    assert event["created"] is False
    assert not cplog.exists()
    assert kuma.requests == []  # deploy-time only; never pushes


def test_ensure_sentinel_creates_a_missing_sentinel_and_reads_it_back(
        files: dict[str, Path], kuma: Any, capsys: pytest.CaptureFixture[str]) -> None:
    cplog = fake_store(files, None)
    rc, event, _ = run_main(args_for(files, kuma, mode="ensure-sentinel"), capsys)
    assert rc == probe.EXIT_OK, event
    assert event["created"] is True
    assert (files["tmp"] / "store").read_bytes() == EXPECTED
    assert len(cplog.read_text().splitlines()) == 1


def test_ensure_sentinel_never_overwrites_foreign_content(
        files: dict[str, Path], kuma: Any, capsys: pytest.CaptureFixture[str]) -> None:
    cplog = fake_store(files, b"not ours\n")
    rc, event, _ = run_main(args_for(files, kuma, mode="ensure-sentinel"), capsys)
    assert rc == probe.EXIT_NFS_FAILED
    assert "unexpected content" in event["msg"]
    assert not cplog.exists()
    assert (files["tmp"] / "store").read_bytes() == b"not ours\n"


def test_ensure_sentinel_does_not_write_when_the_read_hangs(
        files: dict[str, Path], kuma: Any, capsys: pytest.CaptureFixture[str]) -> None:
    cplog = fake_store(files, None, cat_extra="trap '' TERM\nfor _ in $(seq 60); do sleep 1; done\n")
    rc, event, elapsed = run_main(args_for(files, kuma, mode="ensure-sentinel", timeout=1.0),
                                  capsys)
    assert rc == probe.EXIT_NFS_FAILED
    assert elapsed < 3.0
    assert not cplog.exists()


def test_ensure_sentinel_does_not_write_on_permission_errors(
        files: dict[str, Path], kuma: Any, capsys: pytest.CaptureFixture[str]) -> None:
    cplog = files["tmp"] / "cp-calls"
    fake_exe(files, "nfs-cat", "echo 'NFS3ERR_ACCES(-13)' >&2\nexit 10\n")
    fake_exe(files, "nfs-cp", f"echo \"$@\" >> {cplog}\n")
    rc, event, _ = run_main(args_for(files, kuma, mode="ensure-sentinel"), capsys)
    assert rc == probe.EXIT_NFS_FAILED
    assert "NFS3ERR_ACCES" in event["msg"]
    assert not cplog.exists()


def test_ensure_sentinel_reports_a_failed_create(
        files: dict[str, Path], kuma: Any, capsys: pytest.CaptureFixture[str]) -> None:
    fake_store(files, None)
    fake_exe(files, "nfs-cp", "echo 'Failed to creat file /x: NFS3ERR_ROFS(-30)' >&2\nexit 10\n")
    rc, event, _ = run_main(args_for(files, kuma, mode="ensure-sentinel"), capsys)
    assert rc == probe.EXIT_NFS_FAILED
    assert "creating" in event["msg"] and "NFS3ERR_ROFS" in event["msg"]


# ---------------------------------------------------------------------------
# The real libnfs client against fake_nfs_server.py
# ---------------------------------------------------------------------------

def libnfs() -> tuple[Path, Path, dict[str, str]] | None:
    root = os.environ.get("KUMA_PROBE_LIBNFS_ROOT")
    if root:
        base = Path(root)
        cat, cp = base / "usr/bin/nfs-cat", base / "usr/bin/nfs-cp"
        env = {"LD_LIBRARY_PATH": str(base / "usr/lib/x86_64-linux-gnu")}
    else:
        cat, cp, env = Path("/usr/bin/nfs-cat"), Path("/usr/bin/nfs-cp"), {}
    if cat.is_file() and cp.is_file():
        return cat, cp, env
    return None


requires_libnfs = pytest.mark.skipif(
    libnfs() is None,
    reason="real libnfs client not available: install libnfs-utils or set KUMA_PROBE_LIBNFS_ROOT")


@pytest.fixture
def real(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    found = libnfs()
    assert found is not None
    cat, cp, env = found
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return cat, cp


def real_args(files: dict[str, Path], kuma: Any, real: tuple[Path, Path], port: int,
              mode: str = "probe", timeout: float = 5.0) -> list[str]:
    argv = args_for(files, kuma, mode=mode, timeout=timeout, nfs_cat=real[0], nfs_cp=real[1],
                    extra=["--nfs-port", str(port), "--mount-port", str(port)])
    argv[argv.index("--server") + 1] = "127.0.0.1"
    return argv


def leftover_clients(port: int) -> list[int]:
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_bytes()
        except OSError:
            continue
        if f"nfsport={port}".encode() in cmdline and pid_alive(int(proc.name)):
            found.append(int(proc.name))
    return found


@requires_libnfs
def test_real_client_reads_the_sentinel(files: dict[str, Path], kuma: Any,
                                        real: tuple[Path, Path],
                                        capsys: pytest.CaptureFixture[str]) -> None:
    state = State(export=EXPORT, files={SENTINEL: EXPECTED})
    with FakeNfsServer(state) as server:
        rc, event, _ = run_main(real_args(files, kuma, real, server.port), capsys)
    assert rc == probe.EXIT_OK, event
    assert kuma.requests[0]["status"] == "up"
    procs = {(prog, proc) for prog, proc, _, _ in state.calls}
    assert (100005, 1) in procs          # MOUNT MNT
    assert (100003, 3) in procs          # NFS LOOKUP
    assert (100003, 6) in procs          # NFS READ: a real read, not a port check
    assert {(uid, gid) for _, _, uid, gid in state.calls} == {(0, 0)}


@requires_libnfs
@pytest.mark.parametrize("stall", ["nfs", "read", "all"])
def test_real_client_on_a_stalled_server_is_down_within_the_bound(
        stall: str, files: dict[str, Path], kuma: Any, real: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str]) -> None:
    # "nfs" is 2026-09-19: MOUNT (what showmount uses) answers, NFS never does.
    state = State(export=EXPORT, files={SENTINEL: EXPECTED}, stall=stall)
    with FakeNfsServer(state) as server:
        rc, event, elapsed = run_main(real_args(files, kuma, real, server.port, timeout=2.0),
                                      capsys)
        leftovers = leftover_clients(server.port)
    assert rc == probe.EXIT_NFS_FAILED
    assert 2.0 <= elapsed < 2.0 + 2.0, elapsed
    assert "did not finish within 2s" in event["msg"]
    assert kuma.requests[0]["status"] == "down"
    assert leftovers == []
    if stall == "nfs":
        assert (100005, 1) in {(p, c) for p, c, _, _ in state.calls}


@requires_libnfs
def test_real_client_on_a_blackhole_listener_is_down_within_the_bound(
        files: dict[str, Path], kuma: Any, real: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str]) -> None:
    sock = blackhole_listener()
    port = sock.getsockname()[1]
    try:
        rc, _, elapsed = run_main(real_args(files, kuma, real, port, timeout=2.0), capsys)
        leftovers = leftover_clients(port)
    finally:
        sock.close()
    assert rc == probe.EXIT_NFS_FAILED
    assert elapsed < 4.0
    assert leftovers == []


@requires_libnfs
def test_real_client_refused_mount_is_down_with_the_reason(
        files: dict[str, Path], kuma: Any, real: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str]) -> None:
    state = State(export=EXPORT, files={SENTINEL: EXPECTED}, deny_mount=True)
    with FakeNfsServer(state) as server:
        rc, event, _ = run_main(real_args(files, kuma, real, server.port), capsys)
    assert rc == probe.EXIT_NFS_FAILED
    assert "MNT3ERR_ACCES" in event["msg"]


@requires_libnfs
def test_real_client_ensure_sentinel_creates_once(files: dict[str, Path], kuma: Any,
                                                  real: tuple[Path, Path],
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    state = State(export=EXPORT)
    with FakeNfsServer(state) as server:
        rc, event, _ = run_main(real_args(files, kuma, real, server.port, mode="ensure-sentinel"),
                                capsys)
        assert rc == probe.EXIT_OK, event
        assert state.files == {SENTINEL: EXPECTED}
        creates = sum(1 for p, c, _, _ in state.calls if (p, c) == (100003, 8))
        rc, event, _ = run_main(real_args(files, kuma, real, server.port, mode="ensure-sentinel"),
                                capsys)
        assert rc == probe.EXIT_OK, event
        assert sum(1 for p, c, _, _ in state.calls if (p, c) == (100003, 8)) == creates == 1
        before = len(state.calls)
        rc, _, _ = run_main(real_args(files, kuma, real, server.port), capsys)
        assert rc == probe.EXIT_OK
        probe_calls = state.calls[before:]
    # The timer's probe itself never writes: no SETATTR, WRITE, CREATE, COMMIT.
    assert (100003, 6) in {(p, c) for p, c, _, _ in probe_calls}
    assert not any(p == 100003 and c in (2, 7, 8, 21) for p, c, _, _ in probe_calls)


def test_probe_script_is_python_3_11_compatible() -> None:
    # vm117 runs Debian 12, python3 3.11. Parse with the 3.11 grammar.
    source = PROBE.read_text()
    ast.parse(source, feature_version=(3, 11))
    assert source.startswith("#!/usr/bin/python3\n")
    assert os.access(PROBE, os.X_OK)


def test_probe_runs_as_a_script(files: dict[str, Path], kuma: Any) -> None:
    fake_exe(files, "nfs-cat", f"printf '%s' '{EXPECTED.decode()}'\n")
    proc = subprocess.run([sys.executable, str(PROBE), *args_for(files, kuma)],
                          capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["ok"] is True
