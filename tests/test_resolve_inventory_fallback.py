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
import re
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


# linux reaches proxmox/nas/uptime_kuma ONLY through children, exactly as
# inventory.static.ini declares them, and four powered-off guests report no
# address because a stopped VM has no guest agent.
INVENTORY_WITH_CHILDREN_AND_STOPPED = """#!/usr/bin/env python3
import json
print(json.dumps({
 "all": {"children": ["linux", "ungrouped"]},
 "linux": {"children": ["proxmox", "nas", "uptime_kuma"], "hosts": ["jellyfin"]},
 "proxmox": {"hosts": ["pve", "pve2"]},
 "nas": {"hosts": ["truenas"]},
 "uptime_kuma": {"hosts": ["uptime-kuma"]},
 "untagged": {"hosts": ["ad", "windows-2022"]},
 "_meta": {"hostvars": {
   "jellyfin": {"ansible_host": "192.168.1.170", "ansible_user": "ladino"},
   "pve": {"ansible_host": "192.168.1.10", "ansible_user": "root"},
   "pve2": {"ansible_host": "192.168.1.11", "ansible_user": "root"},
   "truenas": {"ansible_host": "192.168.1.15", "ansible_user": "truenas_admin"},
   "uptime-kuma": {"ansible_host": "192.168.1.129", "ansible_user": "ladino"},
   "ad": {"ansible_user": "ladino"},
   "windows-2022": {"ansible_user": "ladino"},
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
    # Logged informationally, and NOT treated as degraded: a host with no
    # address and no cache history is indistinguishable from a stopped guest,
    # and counting it as degraded fired the fallback on every run.
    assert "no address for k8cluster1" in log
    assert "live inventory incomplete" not in log


def test_healthy_inventory_does_not_fall_back(sandbox):
    """No needless regression to the cache when every host has an address."""
    args, cache, _log = _run(sandbox, INVENTORY_HEALTHY)
    assert args.strip() == "", (
        f"fell back to cache unnecessarily: INVENTORY_ARGS={args!r}"
    )
    assert "k8cluster1 ansible_host=192.168.1.90" in cache
    assert "nginx1 ansible_host=192.168.1.92" in cache


def test_status_sidecar_is_cleaned_up(sandbox):
    """No scratch file is left behind, whatever its per-process suffix."""
    _root, _binv, repo = sandbox
    _run(sandbox, INVENTORY_AGENT_MISSED)
    leftovers = sorted(
        p.name
        for p in repo.iterdir()
        if ".status" in p.name or ".tmp" in p.name
    )
    assert leftovers == [], f"scratch files left behind: {leftovers}"


def test_scratch_paths_are_per_process(sandbox):
    """A concurrent healthy run must not wipe a degraded run's marker.

    Both timers run as the same user and can overlap, so a shared
    `inventory-cache.ini.status` let a healthy resolver truncate the DEGRADED
    marker a degraded one had just written. The degraded run then saw an empty
    status file, concluded the inventory was complete, and went ahead with the
    addressless inventory: precisely the failure this module exists to stop.

    Asserted structurally rather than by racing two runs. An interleaving test
    would need a sleep in the fake resolver and a truncate timed against it,
    which is exactly the flaky, timing-dependent shape worth avoiding; a
    version of that passed against the buggy code too, so it was dropped.
    """
    text = SCRIPT.read_text()
    assert '"${INVENTORY_CACHE}.status"' not in text, (
        "shared status path: a concurrent run can truncate it"
    )
    assert '"${INVENTORY_CACHE}.tmp"' not in text, (
        "shared staging path: concurrent runs interleave writes into it"
    )
    assert '.status.$$' in text and '.tmp.$$' in text, (
        "scratch paths should carry the pid so concurrent runs cannot collide"
    )


def test_groups_reached_only_through_children_survive(sandbox):
    """nas, proxmox and uptime_kuma must stay children of linux.

    The cache writer used to infer parent/child with
    group_hosts.issubset(groups[linux]), but groups[linux] holds only the hosts
    listed DIRECTLY under linux, never those reached through its children. The
    three groups that exist solely as [linux:children] entries in
    inventory.static.ini therefore failed the subset test and were dropped, so
    truenas, pve, pve2 and uptime-kuma ended up in no managed group and every
    `hosts: linux` play skipped them in silence.
    """
    _args, cache, _log = _run(sandbox, INVENTORY_WITH_CHILDREN_AND_STOPPED, seed_cache=None)
    assert "[linux:children]" in cache
    kids = cache.split("[linux:children]", 1)[1]
    for group in ("nas", "proxmox", "uptime_kuma"):
        assert re.search(rf"^{group}$", kids, re.M), (
            f"{group} missing from [linux:children]; its hosts are unmanaged"
        )
    for host in ("truenas", "pve", "pve2", "uptime-kuma"):
        assert re.search(rf"^{host} ansible_host=", cache, re.M), f"{host} absent"


def test_stopped_vms_do_not_force_the_cache_fallback(sandbox):
    """A powered-off guest has no agent and no address, by design.

    Counting that as an incomplete inventory fired the fallback on every single
    run, which then exposed the children bug above and silently un-managed four
    hosts. Only a host that HAD an address and lost it is degraded.
    """
    args, _cache, log = _run(sandbox, INVENTORY_WITH_CHILDREN_AND_STOPPED, seed_cache=None)
    assert args.strip() == "", (
        f"stopped VMs forced a needless fallback: INVENTORY_ARGS={args!r}"
    )
    assert "live inventory incomplete" not in log


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
