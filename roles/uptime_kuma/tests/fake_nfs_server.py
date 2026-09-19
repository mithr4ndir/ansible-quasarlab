"""A tiny in-memory NFSv3 + MOUNTv3 server over TCP, for driving the real
libnfs client (nfs-cat, nfs-cp) in tests. Not a general NFS server.

It answers only what libnfs 4.x needs to mount an export, look up, stat, read,
create and write one flat directory of files, and it can be told to stall in
the ways a real server does:

    stall="none"          answer everything
    stall="all"           accept TCP connections, read calls, never answer
    stall="nfs"           answer MOUNT (showmount-style checks pass), never
                          answer an NFS call; the 2026-09-19 shape
    stall="read"          answer everything except READ
    deny_mount=True       refuse MNT with MNT3ERR_ACCES

Both programs are served on one port, so a test passes the same port as
nfsport and mountport and no portmapper is involved.

Every call's program, procedure and AUTH_SYS uid/gid are recorded in `calls`.
"""

from __future__ import annotations

import socket
import socketserver
import struct
import threading
from dataclasses import dataclass, field

MOUNT_PROG, NFS_PROG = 100005, 100003
AUTH_NONE, AUTH_SYS = 0, 1
NFS3_OK, NFS3ERR_NOENT, NFS3ERR_EXIST, NFS3ERR_STALE = 0, 2, 17, 70
MNT3_OK, MNT3ERR_NOENT, MNT3ERR_ACCES = 0, 2, 13
NF3REG, NF3DIR = 1, 2
PROC_UNAVAIL = 3

ROOT_FH = b"fake-root-handle"


class Unpacker:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def u32(self) -> int:
        (value,) = struct.unpack_from(">I", self.data, self.pos)
        self.pos += 4
        return value

    def u64(self) -> int:
        (value,) = struct.unpack_from(">Q", self.data, self.pos)
        self.pos += 8
        return value

    def opaque(self) -> bytes:
        length = self.u32()
        value = self.data[self.pos:self.pos + length]
        self.pos += (length + 3) & ~3
        return value

    def fixed(self, length: int) -> bytes:
        value = self.data[self.pos:self.pos + length]
        self.pos += (length + 3) & ~3
        return value


def u32(value: int) -> bytes:
    return struct.pack(">I", value)


def u64(value: int) -> bytes:
    return struct.pack(">Q", value)


def opaque(value: bytes) -> bytes:
    return u32(len(value)) + value + b"\0" * ((4 - len(value) % 4) % 4)


def fattr3(kind: int, size: int, fileid: int) -> bytes:
    mode = 0o755 if kind == NF3DIR else 0o660
    times = (u32(1_700_000_000) + u32(0)) * 3
    return (u32(kind) + u32(mode) + u32(1) + u32(0) + u32(0) + u64(size) + u64(size)
            + u32(0) + u32(0) + u64(1) + u64(fileid) + times)


def post_op_attr(attr: bytes | None) -> bytes:
    return u32(0) if attr is None else u32(1) + attr


def wcc(attr: bytes | None) -> bytes:
    return u32(0) + post_op_attr(attr)


@dataclass
class State:
    export: str
    files: dict[str, bytes] = field(default_factory=dict)
    stall: str = "none"
    deny_mount: bool = False
    calls: list[tuple[int, int, int | None, int | None]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    release: threading.Event = field(default_factory=threading.Event)

    def file_fh(self, name: str) -> bytes:
        return b"fake-file:" + name.encode()

    def name_of(self, fh: bytes) -> str | None:
        return fh[len(b"fake-file:"):].decode() if fh.startswith(b"fake-file:") else None

    def attr_of(self, fh: bytes) -> bytes | None:
        if fh == ROOT_FH:
            return fattr3(NF3DIR, 4096, 1)
        name = self.name_of(fh)
        if name is None or name not in self.files:
            return None
        return fattr3(NF3REG, len(self.files[name]), 1000 + sorted(self.files).index(name))


class Handler(socketserver.BaseRequestHandler):
    server: "FakeNfsServer"

    def recv_exact(self, count: int) -> bytes:
        buf = b""
        while len(buf) < count:
            chunk = self.request.recv(count - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def handle(self) -> None:
        state = self.server.state
        try:
            while True:
                record = b""
                last = False
                while not last:
                    (marker,) = struct.unpack(">I", self.recv_exact(4))
                    last = bool(marker & 0x80000000)
                    record += self.recv_exact(marker & 0x7FFFFFFF)
                reply = self.dispatch(state, record)
                if reply is None:
                    # Stalled: keep the connection open and answer nothing,
                    # like a server whose nfsd threads are all blocked.
                    state.release.wait()
                    return
                self.request.sendall(u32(0x80000000 | len(reply)) + reply)
        except (ConnectionError, OSError, struct.error):
            return

    def dispatch(self, state: State, record: bytes) -> bytes | None:
        call = Unpacker(record)
        xid, _mtype, _rpcvers, prog, _vers, proc = (call.u32() for _ in range(6))
        flavor, cred = call.u32(), call.opaque()
        call.u32(), call.opaque()  # verifier
        uid = gid = None
        if flavor == AUTH_SYS:
            c = Unpacker(cred)
            c.u32()
            c.opaque()  # stamp, machine name
            uid, gid = c.u32(), c.u32()
        with state.lock:
            state.calls.append((prog, proc, uid, gid))

        if state.stall == "all":
            return None
        if state.stall == "nfs" and prog == NFS_PROG and proc != 0:
            return None
        if state.stall == "read" and prog == NFS_PROG and proc == 6:
            return None

        head = u32(xid) + u32(1) + u32(0) + u32(AUTH_NONE) + u32(0)
        body = self.mount(state, proc, call) if prog == MOUNT_PROG else self.nfs(state, proc, call)
        if body is None:
            return head + u32(PROC_UNAVAIL)
        return head + u32(0) + body

    def mount(self, state: State, proc: int, call: Unpacker) -> bytes | None:
        if proc in (0, 3):  # NULL, UMNT
            return b""
        if proc == 1:  # MNT
            path = call.opaque().decode()
            if state.deny_mount:
                return u32(MNT3ERR_ACCES)
            if path.rstrip("/") != state.export.rstrip("/"):
                return u32(MNT3ERR_NOENT)
            return u32(MNT3_OK) + opaque(ROOT_FH) + u32(1) + u32(AUTH_SYS)
        if proc == 5:  # EXPORT
            return u32(1) + opaque(state.export.encode()) + u32(0) + u32(0)
        return None

    def nfs(self, state: State, proc: int, call: Unpacker) -> bytes | None:
        if proc == 0:  # NULL
            return b""
        fh = call.opaque()
        attr = state.attr_of(fh)
        if proc == 1:  # GETATTR
            return u32(NFS3_OK) + attr if attr is not None else u32(NFS3ERR_STALE)
        if proc == 2:  # SETATTR (truncate on create)
            return u32(NFS3_OK) + wcc(attr)
        if proc == 3:  # LOOKUP
            name = call.opaque().decode()
            dir_attr = post_op_attr(state.attr_of(ROOT_FH))
            if fh != ROOT_FH or name not in state.files:
                return u32(NFS3ERR_NOENT) + dir_attr
            child = state.file_fh(name)
            return u32(NFS3_OK) + opaque(child) + post_op_attr(state.attr_of(child)) + dir_attr
        if proc == 4:  # ACCESS
            wanted = call.u32()
            return u32(NFS3_OK) + post_op_attr(attr) + u32(wanted)
        if proc == 6:  # READ
            offset, count = call.u64(), call.u32()
            name = state.name_of(fh)
            if name is None or name not in state.files:
                return u32(NFS3ERR_STALE) + post_op_attr(None)
            data = state.files[name][offset:offset + count]
            eof = offset + len(data) >= len(state.files[name])
            return u32(NFS3_OK) + post_op_attr(attr) + u32(len(data)) + u32(int(eof)) + opaque(data)
        if proc == 7:  # WRITE
            offset, _count, _stable = call.u64(), call.u32(), call.u32()
            data = call.opaque()
            name = state.name_of(fh)
            if name is None or name not in state.files:
                return u32(NFS3ERR_STALE) + wcc(None)
            with state.lock:
                current = bytearray(state.files[name])
                current[offset:offset + len(data)] = data
                state.files[name] = bytes(current)
            return (u32(NFS3_OK) + wcc(state.attr_of(fh)) + u32(len(data)) + u32(2)
                    + b"verifier")
        if proc == 8:  # CREATE
            name = call.opaque().decode()
            mode = call.u32()
            if fh != ROOT_FH:
                return u32(NFS3ERR_STALE) + wcc(None)
            with state.lock:
                if name in state.files and mode != 0:
                    return u32(NFS3ERR_EXIST) + wcc(state.attr_of(ROOT_FH))
                state.files.setdefault(name, b"")
            child = state.file_fh(name)
            return (u32(NFS3_OK) + u32(1) + opaque(child) + post_op_attr(state.attr_of(child))
                    + wcc(state.attr_of(ROOT_FH)))
        if proc == 19:  # FSINFO
            return (u32(NFS3_OK) + post_op_attr(attr) + u32(65536) * 3 + u32(65536) * 3
                    + u32(4096) + u64(1 << 40) + u32(0) + u32(1) + u32(0x1B))
        if proc == 21:  # COMMIT
            return u32(NFS3_OK) + wcc(attr) + b"verifier"
        return None


class FakeNfsServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, state: State) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.state = state
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def __enter__(self) -> "FakeNfsServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.state.release.set()
        self.shutdown()
        self.server_close()


def blackhole_listener() -> socket.socket:
    """A socket that is listening but never accepts: connect() succeeds
    through the kernel backlog, and nothing is ever read or answered."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    return sock
