"""Tests for the NVIDIA purge in roles/k8s/common and the k8s blacklist.

Run from the repo root:
    uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" pytest roles/ -rs

Nothing here runs Ansible, apt or dpkg. The task file and group_vars are parsed
as YAML and asserted against; the package globs are matched with fnmatch, which
is the same matcher ansible.builtin.apt uses to expand a wildcard package name
against the apt cache.

Background: k8cluster1, k8cluster2 and k8cluster3 each carried a full NVIDIA
driver and container-runtime stack that nothing used (no nvidia module loaded,
nvidia-smi could not reach a driver, no /dev/nvidia*, no nvidia runtime in
/etc/containerd/config.toml, no RuntimeClass, no pod requesting an nvidia
resource), measured on the live nodes on 2026-09-17.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

import yaml

ROLE = Path(__file__).resolve().parents[1]
REPO = ROLE.parents[2]
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
GROUP_VARS = REPO / "group_vars" / "k8s.yml"
K8CLUSTER2_VARS = REPO / "host_vars" / "k8cluster2" / "vars.yml"

# Package names taken verbatim from the 2026-09-17 dpkg audit of the three
# nodes. These must all be matched by the configured globs.
MEASURED_PACKAGES = [
    "nvidia-driver-580-server",
    "nvidia-driver-550-server",
    "nvidia-driver-570-server",
    "nvidia-dkms-580-server",
    "nvidia-kernel-common-580-server",
    "nvidia-compute-utils-580-server",
    "nvidia-firmware-580-server-580.159.03",
    "libnvidia-egl-wayland1",
    "nvidia-container-toolkit",
    "nvidia-container-toolkit-base",
    "libnvidia-container-tools",
    "libnvidia-container1",
]

# The rest of the "libnvidia-*-580-server" stack the audit counted but did not
# name individually. These are the standard Ubuntu driver package members, here
# to prove the glob covers the shape rather than a hand-listed set.
REPRESENTATIVE_PACKAGES = [
    "nvidia-utils-580-server",
    "nvidia-kernel-source-580-server",
    "libnvidia-cfg1-580-server",
    "libnvidia-common-580-server",
    "libnvidia-compute-580-server",
    "libnvidia-decode-580-server",
    "libnvidia-encode-580-server",
    "libnvidia-extra-580-server",
    "libnvidia-fbc1-580-server",
    "libnvidia-gl-580-server",
]

# Nothing in this list may ever be caught by the purge.
MUST_NOT_MATCH = [
    "kubelet",
    "kubeadm",
    "kubectl",
    "kubernetes-cni",
    "cri-tools",
    "containerd.io",
    "containerd",
    "linux-image-6.8.0-124-generic",
    "linux-headers-6.8.0-124-generic",
    "linux-image-generic",
    "qemu-guest-agent",
    "chrony",
    "prometheus-node-exporter",
    "wazuh-agent",
]

EXPECTED_BLACKLIST = [
    "kubelet",
    "kubeadm",
    "kubectl",
    "kubernetes-cni",
    "cri-tools",
    "containerd.io",
    "linux-image-*",
    "linux-headers-*",
]


class Loader(yaml.SafeLoader):
    """SafeLoader that tolerates Ansible's !vault tag (host_vars/jellyfin)."""


Loader.add_constructor("!vault", lambda loader, node: loader.construct_scalar(node))


def load_yaml(path: Path):
    return yaml.load(path.read_text(), Loader=Loader)


def walk(tasks):
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from walk(task.get(key, []) or [])


def purge_task() -> dict:
    matches = [
        t
        for t in walk(load_yaml(TASKS))
        if "ansible.builtin.apt" in t and t["ansible.builtin.apt"].get("state") == "absent"
    ]
    assert len(matches) == 1, f"expected exactly one apt-absent task, got {len(matches)}"
    return matches[0]


def when_list(task: dict) -> list[str]:
    when = task.get("when")
    assert when is not None, f"task {task['name']!r} has no when: guard at all"
    if isinstance(when, str):
        when = [when]
    return [" ".join(str(c).split()) for c in when]


def purge_globs() -> list[str]:
    return load_yaml(DEFAULTS)["k8s_nvidia_purge_packages"]


# ---------------------------------------------------------------------------
# The purge task itself
# ---------------------------------------------------------------------------


def test_purge_uses_the_apt_module_with_purge_and_autoremove() -> None:
    apt = purge_task()["ansible.builtin.apt"]
    assert apt["state"] == "absent"
    assert apt["purge"] is True
    assert apt["autoremove"] is True
    assert apt["name"] == "{{ k8s_nvidia_purge_packages }}"


def test_purge_never_shells_out_to_apt() -> None:
    for task in walk(load_yaml(TASKS)):
        for module in ("ansible.builtin.shell", "ansible.builtin.command", "shell", "command"):
            text = str(task.get(module, ""))
            assert "apt-get" not in text, task.get("name")
            assert "dpkg " not in text, task.get("name")


def test_purge_is_gated_on_the_opt_in_variable() -> None:
    conditions = when_list(purge_task())
    assert "k8s_purge_nvidia | default(false)" in conditions, conditions
    assert any("k8s_purge_nvidia" in c for c in conditions)


def test_purge_is_gated_on_the_node_having_no_gpu() -> None:
    conditions = when_list(purge_task())
    assert "not (nvidia_gpu_node | default(true))" in conditions, conditions
    # The default must be true: an unlabelled node is assumed to have a GPU and
    # is left alone. default(false) here would purge any host the role reaches.
    gpu = next(c for c in conditions if "nvidia_gpu_node" in c)
    assert "default(true)" in gpu, gpu
    assert gpu.startswith("not "), gpu


def test_purge_has_exactly_the_two_guards() -> None:
    assert len(when_list(purge_task())) == 2


def test_purge_is_tagged_so_it_can_be_run_without_the_rest_of_bootstrap() -> None:
    # roles/k8s/common is only reachable through playbooks/k8s_init.yml, which
    # also runs kube_init. The tag is what makes --tags nvidia_purge safe.
    assert "nvidia_purge" in purge_task()["tags"]


def test_purge_runs_before_the_cdi_refresh_handling() -> None:
    tasks = load_yaml(TASKS)
    purge_index = next(
        i for i, t in enumerate(tasks) if t.get("name") == purge_task()["name"]
    )
    cdi_index = next(i for i, t in enumerate(tasks) if "nvidia-cdi-refresh" in t.get("name", ""))
    assert purge_index < cdi_index, (
        "purge must run first so the cdi-refresh probe finds nothing left to mask"
    )


# ---------------------------------------------------------------------------
# The package set
# ---------------------------------------------------------------------------


def test_every_measured_package_is_matched() -> None:
    globs = purge_globs()
    for package in MEASURED_PACKAGES + REPRESENTATIVE_PACKAGES:
        assert any(fnmatch(package, g) for g in globs), f"{package} would survive the purge"


def test_all_three_driver_series_are_matched() -> None:
    globs = purge_globs()
    for series in ("550", "570", "580"):
        package = f"nvidia-driver-{series}-server"
        assert any(fnmatch(package, g) for g in globs), package


def test_nothing_unrelated_is_matched() -> None:
    globs = purge_globs()
    for package in MUST_NOT_MATCH:
        hits = [g for g in globs if fnmatch(package, g)]
        assert not hits, f"{package} would be purged by {hits}"


def test_every_glob_is_justified_by_a_real_package_name() -> None:
    # ansible.builtin.apt expands a wildcard with fnmatch against the whole apt
    # cache and calls fail_json("No package(s) matching ...") when it matches
    # nothing (ansible/modules/apt.py, expand_pkgspec_from_fnmatches). A glob
    # invented on a hunch therefore does not quietly match zero packages, it
    # hard-fails the play on the node. Every glob must name a package that
    # really exists.
    known = MEASURED_PACKAGES + REPRESENTATIVE_PACKAGES
    for glob in purge_globs():
        assert any(fnmatch(package, glob) for package in known), (
            f"{glob} matches no known package; apt would fail the task"
        )


def test_the_globs_are_scoped_to_nvidia_names() -> None:
    for glob in purge_globs():
        assert re.match(r"^(lib)?nvidia-", glob), glob
        assert not glob.startswith("*"), glob


# ---------------------------------------------------------------------------
# Variable wiring
# ---------------------------------------------------------------------------


def test_role_default_does_not_purge() -> None:
    assert load_yaml(DEFAULTS)["k8s_purge_nvidia"] is False


def test_k8s_group_opts_in() -> None:
    assert load_yaml(GROUP_VARS)["k8s_purge_nvidia"] is True


def test_k8s_group_declares_no_gpu_so_the_purge_reaches_all_three_nodes() -> None:
    # Before this change nvidia_gpu_node was set only in host_vars/k8cluster2,
    # so on k8cluster1 and k8cluster3 it was undefined and `| default(true)`
    # made every no-GPU branch in this role skip silently. Without the group
    # level setting the purge would reach one node out of three.
    assert load_yaml(GROUP_VARS)["nvidia_gpu_node"] is False


def test_k8cluster2_host_vars_still_agree_with_the_group() -> None:
    assert load_yaml(K8CLUSTER2_VARS)["nvidia_gpu_node"] is False


# ---------------------------------------------------------------------------
# unattended-upgrades blacklist
# ---------------------------------------------------------------------------


def test_blacklist_no_longer_blocks_nvidia() -> None:
    blacklist = load_yaml(GROUP_VARS)["unattended_upgrades_blacklist"]
    assert not [entry for entry in blacklist if "nvidia" in entry], blacklist


def test_blacklist_keeps_the_kubernetes_and_kernel_entries_exactly() -> None:
    assert load_yaml(GROUP_VARS)["unattended_upgrades_blacklist"] == EXPECTED_BLACKLIST


def test_jellyfin_keeps_its_nvidia_blacklist() -> None:
    # The jellyfin VM is the one host that really owns the passed-through GPU,
    # so its driver must stay pinned against unattended upgrades.
    jellyfin = load_yaml(REPO / "host_vars" / "jellyfin" / "vars.yml")
    blacklist = jellyfin["unattended_upgrades_blacklist"]
    assert "nvidia-*" in blacklist and "libnvidia-*" in blacklist, blacklist
