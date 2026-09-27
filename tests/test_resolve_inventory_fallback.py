"""Tests for scripts/resolve-inventory.sh address handling.

Run from the repo root:
    uv run --python 3.12 --with pytest==8.4.2 --with ansible-core==2.16.3 \
        pytest tests/test_resolve_inventory_fallback.py -rs

SAFETY: nothing here can reach Proxmox or 1Password. PATH is set to a sandbox
bin directory holding fakes for ansible-inventory and curl, so the API
reachability probe and the inventory resolution are both stubbed. A guard test
asserts the real binaries are unreachable.

What these tests pin down:

`ansible_host` is composed from `proxmox_agent_interfaces`, so a guest agent
that misses one reply leaves it undefined for that host. This lab has NO DNS
(pfSense returns NXDOMAIN for every lab name, bare and suffixed), so Ansible
then addresses the host by its inventory hostname and the host is simply
unreachable. That produced `unreachable` runs with zero failed tasks and a
host set that rotated between runs.

The old writer made it worse than a transient: it rewrote the cache entry
WITHOUT an address, destroying the last known good IP, so the fallback that
was supposed to rescue the next run had already been poisoned by this one.

  - a host whose agent went quiet keeps its last known good address
  - such a run switches to the merged cache rather than addressing by hostname
  - a host with no address anywhere is omitted, never written addressless
  - a fully healthy inventory still uses the live one, no needless fallback
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "resolve-inventory.sh"

SEED_CACHE = """[linux]
k8cluster1 ansible_host=192.168.1.90 ansible_user=ladino
nginx1 ansible_host=192.168.1.92 ansible_user=ladino

[linux:children]
k8s
"""

# k8cluster1 has no ansible_host: the guest agent reported no usable IPv4.
INVENTORY_AGENT_MISSED = """#!/usr/bin/env python3
import json
print(json.dumps({
 "linux": {"hosts": ["k8cluster1", "nginx1"]},
 "k8s": {"hosts": ["k8cluster1"]},
 "_meta": {"hostvars": {
    "k8cluster1": {"ansible_user": "ladino"},
    "nginx1": {"ansible_host": "192.168.1.92", "ansible_user": "ladino"},
 }}}))
"""

INVENTORY_HEALTHY = """#!/usr/bin/env python3
import json
print(json.dumps({
 "linux": {"hosts": ["k8cluster1", "nginx1"]},
 "_meta": {"hostvars": {
    "k8cluster1": {"ansible_host": "192.168.1.90", "ansible_user": "ladino"},
    "nginx1": {"ansible_host": "192.168.1.92", "ansible_user": "ladino"},
 }}}))
"""


@pytest.fixture
def sandbox():
    """A throwaway REPO_DIR plus a PATH with only fakes on it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        binv, repo = root / "bin", root / "repo"
        binv.mkdir()
        repo.mkdir()

        # The API reachability probe must never touch the network.
        (binv / "curl").write_text("#!/bin/sh\necho 200\n")
        (binv / "curl").chmod(0o755)

        # coreutils the script actually calls
        for exe in ("bash", "python3", "grep", "awk", "sort", "wc", "date",
                    "stat", "mv", "rm", "chmod", "cat", "sed", "tr"):
            found = shutil.which(exe)
            if found:
                (binv / exe).symlink_to(found)

        yield root, binv, repo


def _run(sandbox, inventory_src, seed_cache=SEED_CACHE):
    root, binv, repo = sandbox
    (binv / "ansible-inventory").write_text(inventory_src)
    (binv / "ansible-inventory").chmod(0o755)
    cache = repo / "inventory-cache.ini"
    if seed_cache is not None:
        cache.write_text(seed_cache)
    logfile = root / "log.txt"
    logfile.touch()

    proc = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; printf "%s" "$INVENTORY_ARGS"'],
        env={
            "PATH": str(binv),
            "REPO_DIR": str(repo),
            "LOGFILE": str(logfile),
            "PROXMOX_TOKEN_SECRET": "dummy-not-a-real-token",
            "HOME": str(root),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout, cache.read_text() if cache.exists() else "", logfile.read_text()


def test_quiet_agent_keeps_last_known_good_address(sandbox):
    """The cache must not lose an address just because the agent went quiet."""
    _args, cache, _log = _run(sandbox, INVENTORY_AGENT_MISSED)
    assert "k8cluster1 ansible_host=192.168.1.90" in cache, (
        "the previous good address was destroyed; the next run has nothing to "
        "fall back to and the host is unreachable with no DNS to save it"
    )


def test_quiet_agent_switches_to_cache_instead_of_hostname(sandbox):
    """A degraded live inventory must not be used: it addresses by hostname."""
    args, _cache, log = _run(sandbox, INVENTORY_AGENT_MISSED)
    assert args.strip().startswith("-i "), (
        f"expected a fallback to the merged cache, got INVENTORY_ARGS={args!r}"
    )
    assert "inventory-cache.ini" in args
    assert "k8cluster1" in log


def test_host_with_no_address_anywhere_is_omitted(sandbox):
    """Never write an addressless host: with no DNS it can never be reached."""
    _args, cache, log = _run(
        sandbox,
        INVENTORY_AGENT_MISSED,
        seed_cache="[linux]\nnginx1 ansible_host=192.168.1.92 ansible_user=ladino\n",
    )
    assert "k8cluster1 ansible_user=" not in cache, (
        "host written without an address; Ansible would address it by hostname"
    )
    assert "k8cluster1 ansible_host=" not in cache
    assert "no address for k8cluster1" in log


def test_healthy_inventory_does_not_fall_back(sandbox):
    """No needless regression to the cache when every host has an address."""
    args, cache, _log = _run(sandbox, INVENTORY_HEALTHY)
    assert args.strip() == "", (
        f"fell back to cache unnecessarily: INVENTORY_ARGS={args!r}"
    )
    assert "k8cluster1 ansible_host=192.168.1.90" in cache
    assert "nginx1 ansible_host=192.168.1.92" in cache


def test_status_sidecar_is_cleaned_up(sandbox):
    """The resolver's status sidecar must not be left behind for the next run."""
    root, _binv, repo = sandbox
    _run(sandbox, INVENTORY_AGENT_MISSED)
    assert not (repo / "inventory-cache.ini.status").exists()
    assert not (repo / "inventory-cache.ini.tmp").exists()


def test_sandbox_cannot_reach_real_binaries(sandbox):
    """Guard: the fakes really do shadow the real tools."""
    _root, binv, _repo = sandbox
    proc = subprocess.run(
        ["bash", "-c", "command -v ansible-playbook op || true"],
        env={"PATH": str(binv)},
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "", (
        f"real tooling reachable from the sandbox: {proc.stdout!r}"
    )
