"""Tests for masking nvidia-persistenced on vfio passthrough hosts.

Run from the repo root:
    uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" pytest roles/ -rs

Nothing here runs Ansible or systemd. The role, its defaults, the two Proxmox
host_vars files and playbooks/proxmox.yml are parsed as YAML and asserted
against.

The defect: pve2 binds its RTX 2080 Ti (10de:1e07 at 0a:00.0) to vfio-pci and
passes it to the jellyfin VM, and blacklists the nvidia/nvidiafb modules so the
host driver can never load. nvidia-persistenced was left installed and enabled,
so since 2026-08-23 it failed at every boot with "Failed to query NVIDIA
devices ... /dev/nvidia*" and that single unit made systemctl report the whole
hypervisor as degraded.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROLE = Path(__file__).resolve().parents[1]
REPO = ROLE.parents[2]
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
HANDLERS = ROLE / "handlers" / "main.yml"
PLAYBOOK = REPO / "playbooks" / "proxmox.yml"
PVE_VARS = REPO / "host_vars" / "pve" / "vars.yml"
PVE2_VARS = REPO / "host_vars" / "pve2" / "vars.yml"

UNIT = "nvidia-persistenced.service"
MASK_PATH = "/etc/systemd/system/nvidia-persistenced.service"
GUARD = "pve_gpu_passthrough | default(false)"


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text())


def walk(tasks):
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from walk(task.get(key, []) or [])


def when_list(task: dict) -> list[str]:
    when = task.get("when")
    assert when is not None, f"task {task.get('name')!r} has no when: guard at all"
    if isinstance(when, str):
        when = [when]
    return [" ".join(str(c).split()) for c in when]


def persistenced_block() -> dict:
    tasks = load_yaml(TASKS)
    matches = [
        t for t in tasks if "block" in t and "nvidia-persistenced" in t.get("name", "")
    ]
    assert len(matches) == 1, f"expected one nvidia-persistenced block, got {len(matches)}"
    return matches[0]


def probe_task() -> dict:
    matches = [
        t
        for t in load_yaml(TASKS)
        if "nvidia-persistenced" in str(t.get("ansible.builtin.command", ""))
    ]
    assert len(matches) == 1, f"expected one probe task, got {len(matches)}"
    return matches[0]


def mask_task() -> dict:
    matches = [
        t
        for t in walk(load_yaml(TASKS))
        if "ansible.builtin.file" in t
        and t["ansible.builtin.file"].get("dest") == MASK_PATH
    ]
    assert len(matches) == 1, f"expected one mask task, got {len(matches)}"
    return matches[0]


def stop_task() -> dict:
    matches = [
        t
        for t in walk(persistenced_block()["block"])
        if "ansible.builtin.systemd" in t
        and t["ansible.builtin.systemd"].get("name") == UNIT
    ]
    assert len(matches) == 1, f"expected one stop/disable task, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def test_unit_is_masked_by_symlinking_to_dev_null() -> None:
    spec = mask_task()["ansible.builtin.file"]
    assert spec["src"] == "/dev/null"
    assert spec["dest"] == MASK_PATH
    assert spec["state"] == "link"
    assert spec["force"] is True


def test_masking_reloads_systemd() -> None:
    assert mask_task()["notify"] == "Reload systemd"
    handlers = {h["name"] for h in load_yaml(HANDLERS)}
    assert "Reload systemd" in handlers, handlers


def test_unit_is_stopped_and_disabled_before_it_is_masked() -> None:
    spec = stop_task()["ansible.builtin.systemd"]
    assert spec["state"] == "stopped"
    assert spec["enabled"] is False
    body = persistenced_block()["block"]
    assert body.index(stop_task()) < body.index(mask_task())


def test_stop_tolerates_a_missing_or_already_masked_unit() -> None:
    assert stop_task()["failed_when"] is False


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_block_is_gated_on_passthrough_being_configured() -> None:
    conditions = when_list(persistenced_block())
    assert GUARD in conditions, conditions


def test_block_is_gated_on_the_unit_actually_existing() -> None:
    conditions = when_list(persistenced_block())
    assert any(
        "nvidia_persistenced_check.stdout" in c and "nvidia-persistenced" in c
        for c in conditions
    ), conditions


def test_probe_is_gated_read_only_and_never_fails_the_play() -> None:
    task = probe_task()
    assert GUARD in when_list(task), when_list(task)
    assert task["changed_when"] is False
    assert task["failed_when"] is False


def test_nothing_in_the_role_touches_the_unit_outside_the_guarded_block() -> None:
    tasks = load_yaml(TASKS)
    block = next(t for t in tasks if "block" in t and "nvidia-persistenced" in t.get("name", ""))
    probe = next(
        t for t in tasks if "nvidia-persistenced" in str(t.get("ansible.builtin.command", ""))
    )
    allowed = [probe, block, *walk(block["block"])]
    for task in walk(tasks):
        if any(task is a for a in allowed):
            continue
        assert "nvidia-persistenced" not in str(task), task.get("name")


# ---------------------------------------------------------------------------
# Where this applies
# ---------------------------------------------------------------------------


def test_role_default_is_no_passthrough() -> None:
    assert load_yaml(DEFAULTS)["pve_gpu_passthrough"] is False


def test_role_only_runs_on_hosts_with_passthrough_enabled() -> None:
    plays = load_yaml(PLAYBOOK)
    roles = [r for play in plays for r in play.get("roles", []) if isinstance(r, dict)]
    entry = next(r for r in roles if r["role"] == "pve/gpu_passthrough")
    assert entry["when"] == "pve_gpu_passthrough | default(false)"


def test_pve2_is_the_only_passthrough_host() -> None:
    assert load_yaml(PVE2_VARS)["pve_gpu_passthrough"] is True
    assert load_yaml(PVE_VARS)["pve_gpu_passthrough"] is False


def test_pve2_still_blacklists_the_nvidia_modules() -> None:
    # The mask is only correct because the host driver can never load.
    blacklist = load_yaml(PVE2_VARS)["pve_gpu_blacklist"]
    assert "nvidia" in blacklist and "nvidiafb" in blacklist, blacklist
