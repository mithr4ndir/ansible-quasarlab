#!/usr/bin/env python3
"""Converge an apt signed-by keyring onto exactly one pinned OpenPGP key.

Why this exists: a plain get_url of a vendor key is a one-shot. Once the file
is on disk it is never re-fetched, so when the vendor rotates its signing key
the keyring goes stale and every `apt-get update` on the host fails with
NO_PUBKEY. HashiCorp did exactly that on 2026-09-10.

Subcommands:

  check    Exit 0 when the keyring holds exactly the pinned primary key and
           nothing else. Exit 1 when it is missing, unreadable, stale, or
           holds extra keys. Nothing is written.

  install  Import a downloaded key file into a throwaway GnuPG home, refuse
           (exit 3) unless the pinned fingerprint is present, then export ONLY
           that key and atomically replace the keyring. Extra keys served
           alongside it are never trusted.

gpg runs with a private temporary --homedir so nothing touches root's
~/.gnupg. Arguments are passed as argv lists, never through a shell.
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

EXIT_OK = 0
EXIT_NEEDS_INSTALL = 1
EXIT_USAGE = 2
EXIT_FINGERPRINT_MISMATCH = 3

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
    """Return the fingerprint of every primary key in --with-colons output.

    Each `pub` record is followed by its own `fpr` record. Subkey `fpr`
    records follow `sub` records and are deliberately ignored, so a vendor
    key with signing subkeys still compares equal to its primary fingerprint.
    """
    fingerprints: list[str] = []
    expecting_primary = False
    for line in colon_output.splitlines():
        fields = line.split(":")
        record = fields[0]
        if record == "pub":
            expecting_primary = True
        elif record == "fpr" and expecting_primary and len(fields) > 9:
            fingerprints.append(fields[9].upper())
            expecting_primary = False
        elif record in ("sub", "uid", "sig", "rev"):
            expecting_primary = False
    return fingerprints


def keyring_fingerprints(homedir: str, keyring: Path) -> list[str] | None:
    result = run_gpg(homedir, "--with-colons", "--show-keys", str(keyring))
    if result.returncode != 0:
        return None
    return primary_fingerprints(result.stdout.decode("utf-8", "replace"))


def cmd_check(keyring: Path, fingerprint: str) -> int:
    if not keyring.is_file():
        print(f"missing: {keyring}")
        return EXIT_NEEDS_INSTALL
    with tempfile.TemporaryDirectory(prefix="apt-pinned-key-") as homedir:
        found = keyring_fingerprints(homedir, keyring)
    if found is None:
        print(f"unreadable: {keyring}")
        return EXIT_NEEDS_INSTALL
    if found != [fingerprint]:
        print(f"stale: {keyring} holds {found or 'no keys'}, want [{fingerprint}]")
        return EXIT_NEEDS_INSTALL
    print(f"ok: {keyring} holds {fingerprint}")
    return EXIT_OK


def cmd_install(source: Path, keyring: Path, fingerprint: str) -> int:
    with tempfile.TemporaryDirectory(prefix="apt-pinned-key-") as homedir:
        imported = run_gpg(homedir, "--import", str(source))
        if imported.returncode != 0:
            sys.stderr.write(imported.stderr.decode("utf-8", "replace"))
            print(f"could not import {source}", file=sys.stderr)
            return EXIT_USAGE

        listed = run_gpg(homedir, "--with-colons", "--list-keys")
        served = primary_fingerprints(listed.stdout.decode("utf-8", "replace"))
        if fingerprint not in served:
            print(
                f"refusing to install: {source} does not contain pinned key "
                f"{fingerprint} (it holds {served or 'no keys'}). Confirm the "
                "vendor's current fingerprint from its official documentation "
                "before changing the pin.",
                file=sys.stderr,
            )
            return EXIT_FINGERPRINT_MISMATCH

        exported = run_gpg(homedir, "--export", fingerprint)
        if exported.returncode != 0 or not exported.stdout:
            sys.stderr.write(exported.stderr.decode("utf-8", "replace"))
            print(f"could not export {fingerprint}", file=sys.stderr)
            return EXIT_USAGE

    keyring.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{keyring.name}.", dir=keyring.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(exported.stdout)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, keyring)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise

    verify = cmd_check(keyring, fingerprint)
    if verify != EXIT_OK:
        print(f"installed keyring failed verification: {keyring}", file=sys.stderr)
        return EXIT_USAGE
    print(f"changed: {keyring} now holds {fingerprint}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check")
    check.add_argument("--keyring", type=Path, required=True)
    check.add_argument("--fingerprint", type=normalize_fingerprint, required=True)

    install = sub.add_parser("install")
    install.add_argument("--source", type=Path, required=True)
    install.add_argument("--keyring", type=Path, required=True)
    install.add_argument("--fingerprint", type=normalize_fingerprint, required=True)

    args = parser.parse_args(argv)
    if shutil.which("gpg") is None:
        print("gpg not found on PATH; install the gnupg package", file=sys.stderr)
        return 2
    if args.command == "check":
        return cmd_check(args.keyring, args.fingerprint)
    return cmd_install(args.source, args.keyring, args.fingerprint)


if __name__ == "__main__":
    sys.exit(main())
