"""Regression tests for the node_exporter install guard (#225).

Run from the repo root with the pinned runner:
    uv run --with pytest --with pyyaml --with "ansible-core==2.16.3" \
      pytest tests/test_node_exporter_upgrade.py

The defect: download, extract and install were all guarded on
`not node_exporter_bin.stat.exists`, so bumping node_exporter_version changed
nothing on any host that already had a binary. The whole fleet stayed on 1.7.0,
whose netlink arp collector fails every scrape on the Calico k8s nodes.

No playbook is run. The role's own task file is loaded and each install task's
`when` goes through Ansible's Conditional, and the set_fact expression through
its Templar, with the registered results a real run would produce. The version
strings are the exact `--version` stdout of the 1.7.0 and 1.12.1 release
binaries, captured on k8cluster1.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1] / "roles" / "monitoring" / "node_exporter"
# Overridable so the same cases can be pointed at an older task file to show
# they fail against the broken guard.
TASKS = Path(os.environ.get("NODE_EXPORTER_TASKS", ROLE / "tasks" / "main.yml"))
DEFAULTS = ROLE / "defaults" / "main.yml"
HANDLERS = ROLE / "handlers" / "main.yml"

ansible = pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.playbook.conditional import Conditional  # noqa: E402
from ansible.plugins.loader import init_plugin_loader  # noqa: E402
from ansible.template import Templar  # noqa: E402

init_plugin_loader()

V170 = (
    "node_exporter, version 1.7.0 (branch: HEAD, revision: "
    "7333465abf9efba81876303bb57e6fadb946041b)\n"
    "  build user:       root@35918982f6d8\n"
    "  build date:       20231112-23:53:35\n"
    "  go version:       go1.21.4\n"
    "  platform:         linux/amd64\n"
    "  tags:             netgo osusergo static_build"
)
V1121 = (
    "node_exporter, version 1.12.1 (branch: HEAD, revision: "
    "6044da783597cc3b57aef7580ddcdcff58a4ee99)\n"
    "  build user:       root@3a6b59999579\n"
    "  build date:       20260714-12:05:11\n"
    "  go version:       go1.26.5\n"
    "  platform:         linux/amd64\n"
    "  tags:             unknown"
)

INSTALL_TASKS = (
    "Download node_exporter",
    "Extract node_exporter",
    "Install node_exporter binary",
)


def _tasks() -> dict[str, dict]:
    return {t["name"]: t for t in yaml.safe_load(TASKS.read_text())}


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _installed_result(scenario: str, version_output: str | None) -> tuple[dict, dict]:
    """What the stat and --version tasks register on a host in this state."""
    if scenario == "missing":
        return {"stat": {"exists": False}}, {"changed": False, "skipped": True}
    return (
        {"stat": {"exists": True}},
        {"changed": False, "rc": 0 if version_output else 127, "stdout": version_output or "", "stderr": ""},
    )


def _host_vars(scenario: str, version_output: str | None, check_mode: bool) -> dict:
    loader = DataLoader()
    bin_result, installed = _installed_result(scenario, version_output)
    variables = dict(
        _defaults(),
        node_exporter_bin=bin_result,
        node_exporter_installed=installed,
        ansible_check_mode=check_mode,
    )
    tasks = _tasks()
    fact_task = tasks.get("Decide whether node_exporter needs installing")
    if fact_task is not None:
        for key, expr in fact_task["ansible.builtin.set_fact"].items():
            variables[key] = Templar(loader=loader, variables=variables).template(expr)
    return variables


def _runs(task: dict, variables: dict) -> bool:
    loader = DataLoader()
    c = Conditional(loader=loader)
    when = task.get("when", [])
    c.when = when if isinstance(when, list) else [when]
    return c.evaluate_conditional(Templar(loader=loader, variables=variables), variables)


@pytest.mark.parametrize(
    ("scenario", "output", "expect_install"),
    [
        ("missing", None, True),
        ("old", V170, True),  # the #225 case: a binary exists but is stale
        ("current", None, False),  # filled in with the pinned version below
        ("broken", "", True),  # binary present but will not run
        ("garbage", "something unexpected", True),
    ],
)
def test_install_runs_exactly_when_version_differs(scenario, output, expect_install):
    if scenario == "current":
        output = V1121.replace("1.12.1", _defaults()["node_exporter_version"])
    variables = _host_vars(scenario, output, check_mode=False)
    tasks = _tasks()
    ran = {name: _runs(tasks[name], variables) for name in INSTALL_TASKS}
    assert ran == {name: expect_install for name in INSTALL_TASKS}


def test_needs_install_is_a_real_boolean():
    # A string "False" would be truthy in a later `when`.
    for output, expected in ((V170, True), (V1121.replace("1.12.1", _defaults()["node_exporter_version"]), False)):
        value = _host_vars("old", output, check_mode=False)["node_exporter_needs_install"]
        assert value is expected


def test_check_mode_reports_download_but_skips_steps_needing_the_archive():
    variables = _host_vars("old", V170, check_mode=True)
    tasks = _tasks()
    assert _runs(tasks["Download node_exporter"], variables)
    assert not _runs(tasks["Extract node_exporter"], variables)
    assert not _runs(tasks["Install node_exporter binary"], variables)


def test_version_probe_is_read_only_and_runs_under_check():
    probe = _tasks()["Read installed node_exporter version"]
    assert probe["changed_when"] is False
    assert probe["check_mode"] is False
    assert probe["failed_when"] is False
    assert probe["ansible.builtin.command"]["argv"][-1] == "--version"


def test_new_binary_restarts_the_service():
    install = _tasks()["Install node_exporter binary"]
    handler_names = {h["name"] for h in yaml.safe_load(HANDLERS.read_text())}
    assert install.get("notify") == "Restart node_exporter"
    assert "Restart node_exporter" in handler_names


def test_download_is_checksum_pinned_and_paths_agree():
    defaults = _defaults()
    assert re.fullmatch(r"[0-9a-f]{64}", defaults["node_exporter_tarball_sha256"])
    tasks = _tasks()
    get_url = tasks["Download node_exporter"]["ansible.builtin.get_url"]
    unarchive = tasks["Extract node_exporter"]["ansible.builtin.unarchive"]
    copy = tasks["Install node_exporter binary"]["ansible.builtin.copy"]
    assert get_url["checksum"] == "sha256:{{ node_exporter_tarball_sha256 }}"
    # A versioned archive path, so a stale tarball from an older version left in
    # /tmp is never extracted in its place.
    assert "{{ node_exporter_version }}" in get_url["dest"]
    assert unarchive["src"] == get_url["dest"]
    assert copy["src"].startswith(unarchive["dest"].rstrip("/") + "/node_exporter-{{ node_exporter_version }}")
