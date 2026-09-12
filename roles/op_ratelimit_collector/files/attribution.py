#!/usr/bin/env python3
"""Fold the op shim invocation log into Prometheus counters.

The op shim (files/op-shim, installed as /usr/local/bin/op) appends one JSON
line per `op` invocation. This script, run by the op-quota-collector timer,
reads the lines appended since its last run, adds them to cumulative counts
kept in a state file, and writes a textfile:

    onepassword_op_invocations_total{consumer,caller,unit,subcommand,slug}

It makes no `op` call and reads nothing but local files.

Byte offsets are tracked per (device, inode), not per path, so rotation
neither loses nor double counts lines. This script is the only rotator: once
the live log passes --max-bytes it is renamed to <log>.1 and a fresh live log
is created. The next run finishes <log>.1 from its saved offset, which picks
up any line a shim appended between the read and the rename.

State is written before the textfile. A crash between the two leaves the
textfile one run stale, never double counted.

Exits 0 on success, 2 on bad arguments or an unwritable state/output path.
Malformed log lines are counted, not fatal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

STATE_VERSION = 1
LABELS = ("consumer", "caller", "unit", "subcommand", "slug")

# SECURITY: label values come from a log that any local op caller can append
# to. Only this allowlist reaches the textfile, so a crafted line cannot
# inject Prometheus syntax (quotes, braces, newlines). Anything else becomes
# "other". fullmatch, not match with $: $ also matches before a trailing
# newline, which would let "read\n" through.
LABEL_ALLOWED = re.compile(r"[A-Za-z0-9_.:@ -]{0,64}")
SESSION_SCOPE = re.compile(r"session-[0-9a-z]+\.scope")


def warn(msg: str) -> None:
    print(f"warn: {msg}", file=sys.stderr)


def clean_label(value: object) -> str:
    if not isinstance(value, str):
        return "other"
    return value if LABEL_ALLOWED.fullmatch(value) else "other"


def normalize_unit(unit: object) -> str:
    """Collapse systemd unit names to a bounded label set.

    ansible-proxmox.service -> ansible-proxmox, session-42.scope -> session.
    """
    if not isinstance(unit, str) or not unit:
        return "unknown"
    if SESSION_SCOPE.fullmatch(unit):
        return "session"
    if unit.startswith("user@"):
        return "user"
    if unit.endswith(".service"):
        return clean_label(unit[: -len(".service")])
    if unit.endswith(".scope"):
        return "scope"
    return "other"


def labels_for(event: dict) -> tuple[str, ...]:
    return (
        clean_label(event.get("consumer")) or "unknown",
        clean_label(event.get("caller")) or "unknown",
        normalize_unit(event.get("unit")),
        clean_label(event.get("subcommand")) or "none",
        clean_label(event.get("slug")),
    )


def file_id(st: os.stat_result) -> str:
    return f"{st.st_dev}:{st.st_ino}"


class State:
    def __init__(self) -> None:
        self.offsets: dict[str, int] = {}
        self.counts: dict[tuple[str, ...], int] = {}
        self.malformed = 0
        self.last_ts = 0

    @classmethod
    def load(cls, path: Path) -> "State":
        state = cls()
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return state
        except (OSError, ValueError) as exc:
            # Starting over resets the counters, which Prometheus handles as
            # a counter reset. Refusing to run would blind the dashboard.
            warn(f"unreadable state {path}, starting fresh: {exc}")
            return state
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            warn(f"unexpected state format in {path}, starting fresh")
            return state
        try:
            state.offsets = {str(k): int(v) for k, v in raw.get("offsets", {}).items()}
            for row in raw.get("counts", []):
                key = tuple(str(v) for v in row["labels"])
                if len(key) == len(LABELS):
                    state.counts[key] = int(row["value"])
            state.malformed = int(raw.get("malformed", 0))
            state.last_ts = int(raw.get("last_ts", 0))
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            warn(f"corrupt state in {path}, starting fresh: {exc}")
            return cls()
        return state

    def dump(self) -> str:
        return json.dumps(
            {
                "version": STATE_VERSION,
                "offsets": self.offsets,
                "counts": [
                    {"labels": list(k), "value": v} for k, v in sorted(self.counts.items())
                ],
                "malformed": self.malformed,
                "last_ts": self.last_ts,
            },
            indent=1,
        )


def consume(path: Path, offset: int, state: State) -> Optional[tuple[str, int]]:
    """Count complete lines in path from offset. Returns (file id, new offset).

    A trailing line without a newline is a shim mid-write; it is left for
    the next run. Returns None if the file does not exist.
    """
    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        return None
    with fh:
        st = os.fstat(fh.fileno())
        if st.st_size < offset:
            # Truncated or replaced under the same inode: start over.
            offset = 0
        fh.seek(offset)
        data = fh.read()
    end = data.rfind(b"\n")
    if end < 0:
        return file_id(st), offset
    for raw in data[: end + 1].splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw.decode("utf-8"))
            if not isinstance(event, dict):
                raise ValueError("not an object")
        except (UnicodeDecodeError, ValueError):
            state.malformed += 1
            continue
        key = labels_for(event)
        state.counts[key] = state.counts.get(key, 0) + 1
        ts = event.get("ts")
        if isinstance(ts, int) and ts > state.last_ts:
            state.last_ts = ts
    return file_id(st), offset + end + 1


def rotate(log: Path, rotated: Path, live_id: str, live_offset: int) -> dict[str, int]:
    """Rename the live log aside and create a fresh one. Returns new offsets."""
    os.replace(log, rotated)
    offsets = {live_id: live_offset}
    try:
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        os.close(fd)
    except FileExistsError:
        pass  # a shim appended first and created it
    try:
        st = os.stat(log)
        offsets.setdefault(file_id(st), 0)
    except FileNotFoundError:
        pass
    return offsets


def run(log: Path, state_path: Path, max_bytes: int) -> State:
    state = State.load(state_path)
    rotated = log.with_name(log.name + ".1")
    offsets: dict[str, int] = {}

    # Finish the rotated file first, but only one we rotated ourselves.
    # An unknown .1 was either already fully counted or predates us.
    try:
        rot_id = file_id(os.stat(rotated))
    except FileNotFoundError:
        rot_id = None
    if rot_id is not None and rot_id in state.offsets:
        result = consume(rotated, state.offsets[rot_id], state)
        if result is not None:
            offsets[result[0]] = result[1]

    live_id = None
    live_offset = 0
    try:
        live_id = file_id(os.stat(log))
    except FileNotFoundError:
        pass
    if live_id is not None:
        result = consume(log, state.offsets.get(live_id, 0), state)
        if result is not None:
            live_id, live_offset = result
            offsets[live_id] = live_offset

    if live_id is not None and live_offset >= max_bytes:
        try:
            offsets = rotate(log, rotated, live_id, live_offset)
        except OSError as exc:
            warn(f"rotation of {log} failed: {exc}")

    state.offsets = offsets
    return state


def render(state: State) -> str:
    out = [
        "# HELP onepassword_op_invocations_total op CLI invocations recorded by the attribution shim.",
        "# TYPE onepassword_op_invocations_total counter",
    ]
    for key, value in sorted(state.counts.items()):
        labels = ",".join(f'{name}="{val}"' for name, val in zip(LABELS, key))
        out.append(f"onepassword_op_invocations_total{{{labels}}} {value}")
    out += [
        "# HELP onepassword_op_shim_malformed_lines_total Unparseable lines in the op shim log.",
        "# TYPE onepassword_op_shim_malformed_lines_total counter",
        f"onepassword_op_shim_malformed_lines_total {state.malformed}",
        "# HELP onepassword_op_shim_last_invocation_timestamp_seconds Unix time of the newest recorded op invocation (0 if none).",
        "# TYPE onepassword_op_shim_last_invocation_timestamp_seconds gauge",
        f"onepassword_op_shim_last_invocation_timestamp_seconds {state.last_ts}",
    ]
    return "\n".join(out) + "\n"


def atomic_write(path: Path, text: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-bytes", type=int, default=5 * 1024 * 1024)
    args = parser.parse_args(argv)
    if args.max_bytes < 1:
        parser.error("--max-bytes must be positive")

    state = run(args.log, args.state, args.max_bytes)
    try:
        atomic_write(args.state, state.dump(), 0o600)
        atomic_write(args.out, render(state), 0o644)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
