#!/usr/bin/env python3
"""Remove specific keys from apt's legacy /etc/apt/trusted.gpg keyring.

Keys in that file are trusted for EVERY source without a signed-by option, so
a decommissioned vendor key left there keeps vouching for any repo that
reappears. apt-key is deprecated, so this uses gpg directly:

  1. read the primary fingerprints in the keyring (no writes)
  2. if none of the target fingerprints are present, report "unchanged"
  3. otherwise import the keyring into a throwaway GnuPG home, delete the
     targets, and either remove the file (nothing left, which is the modern
     default) or atomically replace it with the remaining keys exported

Keys that are not targeted are always kept. Output is one line starting with
"unchanged", "changed" or "removed" so Ansible can derive changed_when.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FINGERPRINT_RE = re.compile(r"^[0-9A-F]{40}$")


def normalize_fingerprint(value: str) -> str:
    fingerprint = value.replace(" ", "").upper()
    if not FINGERPRINT_RE.match(fingerprint):
        raise argparse.ArgumentTypeError(
            f"not a 40 hex digit v4 fingerprint: {value!r}"
        )
    return fingerprint


def run_gpg(homedir: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["gpg", "--homedir", homedir, "--batch", "--no-tty", *args],
        capture_output=True,
        check=False,
    )


def primary_fingerprints(colon_output: str) -> list[str]:
    fingerprints: list[str] = []
    expecting_primary = False
    for line in colon_output.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            expecting_primary = True
        elif fields[0] == "fpr" and expecting_primary and len(fields) > 9:
            fingerprints.append(fields[9].upper())
            expecting_primary = False
        elif fields[0] in ("sub", "uid", "sig", "rev"):
            expecting_primary = False
    return fingerprints


def fail(message: str, result: subprocess.CompletedProcess[bytes] | None = None) -> int:
    if result is not None:
        sys.stderr.write(result.stderr.decode("utf-8", "replace"))
    print(message, file=sys.stderr)
    return 2


def remove_keys(keyring: Path, targets: list[str]) -> int:
    if not keyring.exists():
        print(f"unchanged: {keyring} does not exist")
        return 0

    with tempfile.TemporaryDirectory(prefix="apt-trusted-key-") as homedir:
        shown = run_gpg(homedir, "--with-colons", "--show-keys", str(keyring))
        if shown.returncode != 0:
            # SECURITY: never rewrite a keyring we could not parse.
            return fail(f"could not read {keyring}", shown)
        present = primary_fingerprints(shown.stdout.decode("utf-8", "replace"))
        doomed = [fpr for fpr in targets if fpr in present]
        if not doomed:
            print(f"unchanged: {keyring} holds none of {targets}")
            return 0

        imported = run_gpg(homedir, "--import", str(keyring))
        if imported.returncode != 0:
            return fail(f"could not import {keyring}", imported)
        deleted = run_gpg(homedir, "--yes", "--delete-keys", *doomed)
        if deleted.returncode != 0:
            return fail(f"could not delete {doomed}", deleted)

        listed = run_gpg(homedir, "--with-colons", "--list-keys")
        remaining = primary_fingerprints(listed.stdout.decode("utf-8", "replace"))
        if sorted(remaining) != sorted(fpr for fpr in present if fpr not in doomed):
            return fail(
                f"refusing to rewrite {keyring}: expected to keep "
                f"{[f for f in present if f not in doomed]}, gpg kept {remaining}"
            )

        if not remaining:
            keyring.unlink()
            print(f"removed: {keyring} held only {doomed}")
            return 0

        exported = run_gpg(homedir, "--export", *remaining)
        if exported.returncode != 0 or not exported.stdout:
            return fail(f"could not export remaining keys {remaining}", exported)

    mode = keyring.stat().st_mode & 0o777
    fd, tmp_name = tempfile.mkstemp(prefix=f".{keyring.name}.", dir=keyring.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(exported.stdout)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, keyring)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    print(f"changed: removed {doomed} from {keyring}, kept {remaining}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keyring", type=Path, required=True)
    parser.add_argument(
        "--fingerprint",
        type=normalize_fingerprint,
        action="append",
        required=True,
        dest="fingerprints",
    )
    args = parser.parse_args(argv)
    if shutil.which("gpg") is None:
        print("gpg not found on PATH; install the gnupg package", file=sys.stderr)
        return 2
    return remove_keys(args.keyring, args.fingerprints)


if __name__ == "__main__":
    sys.exit(main())
