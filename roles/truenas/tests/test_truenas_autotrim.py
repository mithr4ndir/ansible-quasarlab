"""The autotrim task has to be selective and idempotent, or it is worse than absent.

Two things make this task risky in opposite directions, and both are pinned here.

1. It must actually fire on a pool whose autotrim is off. The whole defect was
   that nobody had ever told these SSDs which blocks were free, so a task that
   quietly selects nothing restores the original silence.
2. It must select nothing once autotrim is on. The task carries
   `changed_when: true`, because `midclt call pool.update` cannot report
   whether it changed anything, so the loop is the only thing standing between
   this role and a host that reports `changed` on every hourly run forever.
   `AnsiblePlaybookMadeChanges` would then fire permanently, which is how a
   real alert becomes background noise.

The loop expression is read out of tasks/main.yml and rendered with Ansible's
own filters, not reimplemented here. A hand-written copy of the pipeline would
keep passing if the role lost its `rejectattr`, which is exactly the regression
worth catching.
"""

import pathlib

import yaml

ROLE = pathlib.Path(__file__).resolve().parents[1]
TASKS_PATH = ROLE / "tasks" / "main.yml"
DEFAULTS_PATH = ROLE / "defaults" / "main.yml"

TASKS = yaml.safe_load(TASKS_PATH.read_text())
DEFAULTS = yaml.safe_load(DEFAULTS_PATH.read_text())

AUTOTRIM_TASK_NAME = "Enable autotrim on the pools that want it"
QUERY_TASK_NAME = "Read pool autotrim state from the TrueNAS middleware"


def _task(name):
    found = [t for t in TASKS if t.get("name") == name]
    assert len(found) == 1, f"expected exactly one task named {name!r}, got {len(found)}"
    return found[0]


def _pool(name, autotrim, pool_id=1):
    """Shaped like one element of `midclt call pool.query` on TrueNAS SCALE."""
    return {
        "id": pool_id,
        "name": name,
        "autotrim": {
            "value": autotrim,
            "rawvalue": autotrim,
            "parsed": autotrim,
            "source": "DEFAULT" if autotrim == "off" else "LOCAL",
        },
    }


def _selected(pools, wanted):
    """Render the role's real loop expression against a pool.query payload."""
    import json

    from ansible.plugins.filter.core import FilterModule
    from jinja2 import Environment

    env = Environment()
    env.filters.update(FilterModule().filters())

    expr = _task(AUTOTRIM_TASK_NAME)["loop"]
    rendered = env.from_string("{{ (" + expr.strip().strip("{}").strip() + ") | map(attribute='name') | list | to_json }}")
    raw = {
        "stdout": json.dumps(pools),
    }
    out = rendered.render(
        truenas_pool_query_raw=raw,
        truenas_autotrim_pools=wanted,
    )
    return json.loads(out)


def test_the_task_targets_a_pool_whose_autotrim_is_off():
    # The defect itself. If this selects nothing the role is decorative.
    assert _selected([_pool("tank", "off")], ["tank"]) == ["tank"]


def test_a_pool_already_on_is_not_touched_again():
    # Guards the permanent-changed failure mode described in the docstring.
    assert _selected([_pool("tank", "on")], ["tank"]) == []


def test_a_pool_not_in_the_list_is_never_touched():
    # boot-pool is on the same box and is deliberately out of scope.
    pools = [_pool("tank", "off", 1), _pool("boot-pool", "off", 2)]
    assert _selected(pools, ["tank"]) == ["tank"]


def test_mixed_pools_select_only_the_ones_that_need_it():
    pools = [
        _pool("tank", "off", 1),
        _pool("scratch", "on", 2),
        _pool("boot-pool", "off", 3),
    ]
    assert _selected(pools, ["tank", "scratch"]) == ["tank"]


def test_the_query_task_still_reads_under_check_mode():
    # A read that honours check mode returns no stdout, and the loop above then
    # fails to parse rather than selecting nothing, so this is load-bearing.
    query = _task(QUERY_TASK_NAME)
    assert query["changed_when"] is False
    assert query["check_mode"] is False


def test_tank_is_in_the_defaults():
    # The measurement in defaults/main.yml is about tank specifically.
    assert "tank" in DEFAULTS["truenas_autotrim_pools"]


def test_the_update_passes_json_as_one_argument():
    """`cmd` would hand midclt a JSON payload split across two arguments.

    The command module shlex-splits a `cmd` string, which drops the quotes and
    breaks on the space inside `{"autotrim": "ON"}`. Against the real
    middleware that returns
    `[EFAULT] Too many arguments (expected 2, found 3)`, so the task has to use
    the argv list form to keep the payload intact.
    """
    import json
    import shlex

    from ansible.plugins.filter.core import FilterModule
    from jinja2 import Environment

    task = _task(AUTOTRIM_TASK_NAME)["ansible.builtin.command"]
    assert "cmd" not in task, "a cmd string would split the JSON payload"

    argv = task["argv"]
    assert argv[:3] == ["midclt", "call", "pool.update"]
    assert len(argv) == 5, "pool.update takes exactly the id and the payload"

    env = Environment()
    env.filters.update(FilterModule().filters())
    rendered = [env.from_string(a).render(item={"id": 1, "name": "tank"}) for a in argv]

    # The payload survives as one argument and is still parseable JSON.
    assert json.loads(rendered[-1]) == {"autotrim": "ON"}

    # And the reason argv is required: the equivalent cmd string does not.
    as_cmd = " ".join(rendered)
    assert shlex.split(as_cmd) != rendered


def test_no_midclt_task_passes_a_json_payload_as_a_cmd_string():
    """Generalise the argv bug across the whole role.

    Both midclt write tasks in this role originally used `cmd` with a
    `to_json` payload, and both were broken the same way: the command module
    shlex-splits the string, the quotes vanish, and the JSON arrives as several
    arguments. The syslog one hid for as long as it did because its `when`
    guard is false whenever the live config already matches, which is almost
    always. So this asserts the shape for every such task rather than for the
    two that exist today.
    """
    offenders = []
    for task in TASKS:
        spec = task.get("ansible.builtin.command") or {}
        cmd = spec.get("cmd")
        if isinstance(cmd, str) and "to_json" in cmd:
            offenders.append(task.get("name"))
    assert offenders == [], (
        "these tasks pass a JSON payload through a shlex-split cmd string and "
        f"will reach midclt as separate arguments: {offenders}. Use argv."
    )
