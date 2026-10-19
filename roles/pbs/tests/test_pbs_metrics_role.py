"""Role-structure tests for the PBS backup metrics collector.

Two things the shell tests cannot see, both of which are the same mistake in
different clothes: configuration that LOOKS applied and asserts nothing.

1. The script falls back to its own hard-coded paths. A unit that passes
   neither path keeps working on pbs1 and silently ignores an override, so
   the role would create the configured textfile directory while the
   collector wrote somewhere node_exporter never reads.
2. A role that can only turn a feature on is not declarative. Setting
   pbs_metrics_enabled to false after the role has converged once would leave
   the timer enabled and the last .prom on disk, where node_exporter keeps
   serving frozen snapshot timestamps for a feature the operator believes is
   switched off. Same shape as the vzdump job that kept its old
   --prune-backups because the role stopped PASSING the flag instead of
   REMOVING it.
"""

from pathlib import Path

import jinja2
import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1]
TASKS = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
UNIT_TEMPLATE = ROLE / "templates" / "pbs-backup-collector.service.j2"

DISABLED_WHEN = "not (pbs_metrics_enabled | bool)"


def _render(**overrides) -> str:
    """Render the service unit the way the role does."""
    values = {
        "pbs_metrics_datastore_root": DEFAULTS["pbs_metrics_datastore_root"],
        "pbs_metrics_prom_file": "/var/lib/node_exporter/textfiles/pbs_backup.prom",
    }
    values.update(overrides)
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    return env.from_string(UNIT_TEMPLATE.read_text()).render(**values)


def _module(task, name: str):
    """Tasks here are written with full FQCNs, which is the repo convention."""
    for key in (f"ansible.builtin.{name}", name):
        if key in task:
            return task[key]
    return None


def _when(task) -> list:
    w = task.get("when", [])
    return w if isinstance(w, list) else [w]


def _disabled_tasks() -> list:
    return [t for t in TASKS if DISABLED_WHEN in _when(t)]


def test_the_unit_passes_both_configured_paths_to_the_script():
    unit = _render()
    assert f"Environment=PBS_DATASTORE_ROOT={DEFAULTS['pbs_metrics_datastore_root']}" in unit
    assert (
        "Environment=PBS_COLLECTOR_PROM_FILE="
        "/var/lib/node_exporter/textfiles/pbs_backup.prom" in unit
    )


def test_an_overridden_path_reaches_the_unit():
    """The regression: a copied unit renders identically whatever the vars say."""
    unit = _render(
        pbs_metrics_datastore_root="/srv/pbs",
        pbs_metrics_prom_file="/var/lib/node_exporter/textfiles/elsewhere.prom",
    )
    assert "Environment=PBS_DATASTORE_ROOT=/srv/pbs" in unit
    assert (
        "Environment=PBS_COLLECTOR_PROM_FILE="
        "/var/lib/node_exporter/textfiles/elsewhere.prom" in unit
    )
    assert "/mnt/datastore\n" not in unit


def test_the_unit_is_deployed_as_a_template_not_a_copy():
    """A copy task would pass the test above and still ship a fixed unit."""
    installs = [
        t
        for t in TASKS
        if _module(t, "template")
        and "pbs-backup-collector.service"
        in str(_module(t, "template").get("src", ""))
    ]
    assert installs, "the service unit must be templated so the paths reach the script"


def test_the_unit_still_sets_a_start_timeout():
    """A hung oneshot silently ends every future run of its timer."""
    assert "TimeoutStartSec=" in _render()


def test_disabling_metrics_stops_and_disables_the_timer():
    retires = [
        t
        for t in _disabled_tasks()
        if _module(t, "systemd")
        and "pbs-backup-collector.timer" in str(_module(t, "systemd").get("name"))
    ]
    assert retires, f"no task retires the timer when {DISABLED_WHEN}"
    for t in retires:
        assert _module(t, "systemd").get("enabled") is False
        assert _module(t, "systemd").get("state") == "stopped"


def test_disabling_metrics_removes_the_stale_prom_file():
    """The whole point: node_exporter keeps serving a .prom nobody refreshes."""
    removed = set()
    for t in _disabled_tasks():
        mod = _module(t, "file")
        if mod is None or mod.get("state") != "absent":
            continue
        loop = t.get("loop", [])
        removed.update(loop if isinstance(loop, list) else [loop])
        removed.add(str(mod.get("path")))
    assert "{{ pbs_metrics_prom_file }}" in removed, (
        "the metrics file survives being disabled, so the staleness alert "
        f"fires forever: {sorted(removed)}"
    )
    assert "/usr/local/bin/pbs-backup-collector.sh" in removed
    assert "/etc/systemd/system/pbs-backup-collector.timer" in removed


def test_the_retire_tasks_do_not_run_when_metrics_are_enabled():
    """A retire path that ran unconditionally would delete what was just built."""
    assert _disabled_tasks(), "no disabled-state path at all"
    for t in _disabled_tasks():
        assert any("pbs_metrics_enabled" in str(c) for c in _when(t))


@pytest.mark.parametrize(
    "var", ["pbs_metrics_enabled", "pbs_metrics_prom_file", "pbs_metrics_datastore_root"]
)
def test_every_knob_the_unit_and_tasks_reference_has_a_default(var):
    assert var in DEFAULTS
